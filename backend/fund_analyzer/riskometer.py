"""Read a scheme's SEBI riskometer from the picture in its official AMC portfolio file.

AMC files publish the riskometer only as an image. This follows the needle to the
segment it points at, using the picture's own six segments. Anything unclear returns
None, so the page says "Risk Not Available" rather than guessing.
"""
from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

import numpy as np
import pandas as pd
from PIL import Image

logger = logging.getLogger(__name__)

RISK_LEVELS = ('Low', 'Low to Moderate', 'Moderate', 'Moderately High', 'High', 'Very High')
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_SIDE = 4000
SEGMENT_COLOUR_GAP = 38
MIN_SEGMENT_DEGREES = 12
BOUNDARY_MARGIN_DEGREES = 1.5

_NAMESPACES = {
    'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'rel': 'http://schemas.openxmlformats.org/package/2006/relationships',
    'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
}
_DOC_REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_RISK_WORD = r'(?:riskometer|risk\s*-?\s*o\s*-?\s*meter)'


def _dial_box(saturation: np.ndarray, value: np.ndarray) -> tuple[int, int, int, int] | None:
    """Return the dial's left, right, top and baseline rows and columns."""
    dark_rows = (value < 80).mean(axis=1) > 0.55
    colored = (saturation > 90) & (value > 110)
    band_rows = np.flatnonzero(dark_rows & (np.arange(len(dark_rows)) > len(dark_rows) * 0.25))
    if not len(band_rows):
        return None
    band_top = int(band_rows[0])
    columns = np.flatnonzero(colored[:band_top].sum(axis=0) >= 2)
    rows = np.flatnonzero(colored[:band_top].sum(axis=1) >= 2)
    if not len(columns) or not len(rows):
        return None
    return int(columns[0]), int(columns[-1]), int(rows[0]), band_top


def _dial_geometry(saturation: np.ndarray, value: np.ndarray) -> tuple[float, float, float] | None:
    """Return the dial's centre x, centre y and outer radius."""
    box = _dial_box(saturation, value)
    if box is None:
        return None
    left, right, top, band_top = box
    radius = (right - left) / 2
    if radius < 20 or abs((band_top - top) - radius) > radius * 0.2:
        return None
    return (left + right) / 2, float(band_top), float(radius)


def _undo_stretch(image: Image.Image) -> Image.Image:
    """Some AMC files stretch the dial into an oval. Make it a half circle again."""
    hsv = np.asarray(image.convert('HSV'))
    box = _dial_box(hsv[:, :, 1], hsv[:, :, 2])
    if box is None:
        return image
    left, right, top, band_top = box
    if band_top <= top or right <= left:
        return image
    scale = ((right - left) / 2) / (band_top - top)
    # Beyond this it is not a stretched dial, so leave it to fail the circle check.
    if not 0.6 <= scale <= 1.7 or abs(scale - 1) < 0.1:
        return image
    return image.resize((image.width, round(image.height * scale)), Image.Resampling.BILINEAR)


def _ring_segments(rgb: np.ndarray, colored: np.ndarray, centre_x: float, centre_y: float, radius: float) -> tuple[list[tuple[float, float]], float] | None:
    """Walk the arc from left to right and return its colour segments and inner radius."""
    samples: list[tuple[float, np.ndarray | None]] = []
    inner_edges = []
    fractions = np.linspace(0.55, 0.99, 34)
    for degrees in np.arange(179.0, 0.5, -0.5):
        angle = np.radians(degrees)
        xs = np.clip((centre_x + np.cos(angle) * radius * fractions).astype(int), 0, rgb.shape[1] - 1)
        ys = np.clip((centre_y - np.sin(angle) * radius * fractions).astype(int), 0, rgb.shape[0] - 1)
        hits = colored[ys, xs]
        if hits.sum() >= 3:
            samples.append((float(degrees), np.median(rgb[ys, xs][hits], axis=0)))
            inner_edges.append(fractions[np.argmax(hits)] * radius)
        else:
            samples.append((float(degrees), None))
    if not inner_edges:
        return None

    segments: list[dict] = []
    for degrees, colour in samples:
        if colour is None:
            continue
        if segments and np.abs(segments[-1]['colour'] - colour).sum() < SEGMENT_COLOUR_GAP * 1.6:
            segments[-1]['end'] = degrees
        else:
            segments.append({'start': degrees, 'end': degrees, 'colour': colour})
    # Antialiased edges and text make slivers; only real segments are wide.
    wide = [segment for segment in segments if segment['start'] - segment['end'] >= MIN_SEGMENT_DEGREES]
    merged: list[dict] = []
    for segment in wide:
        if merged and np.abs(merged[-1]['colour'] - segment['colour']).sum() < SEGMENT_COLOUR_GAP * 1.6:
            merged[-1]['end'] = segment['end']
        else:
            merged.append(dict(segment))
    if len(merged) != len(RISK_LEVELS):
        return None
    return [(segment['start'], segment['end']) for segment in merged], float(np.median(inner_edges))


def _needle_angle(saturation: np.ndarray, value: np.ndarray, centre_x: float, centre_y: float, inner_radius: float) -> float | None:
    """Fit a line through the needle and extend it to the arc."""
    ys, xs = np.nonzero((value[: int(centre_y) - 1] < 150) & (saturation[: int(centre_y) - 1] < 110))
    distances = np.hypot(xs - centre_x, ys - centre_y)
    keep = (distances > inner_radius * 0.15) & (distances < inner_radius * 1.02)
    if keep.sum() < 12:
        return None
    points = np.column_stack((xs[keep], ys[keep])).astype(float)
    middle = points.mean(axis=0)
    _, singular, vectors = np.linalg.svd(points - middle, full_matrices=False)
    # A needle is long and thin. A round blob or stray marks have no clear direction.
    if singular[1] == 0 or singular[0] / singular[1] < 1.8:
        return None
    direction = vectors[0]
    # Point the line outwards from the dial centre.
    if np.dot(direction, middle - np.array((centre_x, centre_y))) < 0:
        direction = -direction
    projection = (points - middle) @ direction
    tip = middle + direction * projection.max()
    offset = tip - np.array((centre_x, centre_y))
    b = np.dot(offset, direction)
    target = inner_radius * 1.08
    c = np.dot(offset, offset) - target ** 2
    step = -b + np.sqrt(max(b * b - c, 0.0))
    hit = tip + direction * step
    return float(np.degrees(np.arctan2(centre_y - hit[1], hit[0] - centre_x)))


def read_risk_level(image_bytes: bytes) -> str | None:
    """Return the level the riskometer picture points at, or None if it is unclear."""
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        return None
    try:
        image = Image.open(BytesIO(image_bytes))
        if max(image.size) > MAX_IMAGE_SIDE:
            return None
        if image.mode in ('RGBA', 'LA', 'P'):
            image = image.convert('RGBA')
            background = Image.new('RGBA', image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image)
        rgb_image = _undo_stretch(image.convert('RGB'))
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    hsv = np.asarray(rgb_image.convert('HSV'))
    rgb = np.asarray(rgb_image).astype(float)
    saturation, value = hsv[:, :, 1], hsv[:, :, 2]
    geometry = _dial_geometry(saturation, value)
    if geometry is None:
        return None
    centre_x, centre_y, radius = geometry
    colored = (saturation > 90) & (value > 110)
    ring = _ring_segments(rgb, colored, centre_x, centre_y, radius)
    if ring is None:
        return None
    segments, inner_radius = ring
    angle = _needle_angle(saturation, value, centre_x, centre_y, inner_radius)
    if angle is None:
        return None
    # Segment edges are where the colour changes, so use the midpoint of each gap.
    edges = [180.0, *((segments[index][1] + segments[index + 1][0]) / 2 for index in range(len(segments) - 1)), 0.0]
    for index, level in enumerate(RISK_LEVELS):
        high, low = edges[index], edges[index + 1]
        if low <= angle <= high:
            near_inner_edge = (index > 0 and high - angle < BOUNDARY_MARGIN_DEGREES) or (index < len(RISK_LEVELS) - 1 and angle - low < BOUNDARY_MARGIN_DEGREES)
            return None if near_inner_edge else level
    return None


def _sheet_pictures(archive: zipfile.ZipFile, sheet_name: str) -> list[tuple[int, int, str]]:
    """Return (row, column, media path) for each picture on one named sheet."""
    workbook = ElementTree.fromstring(archive.read('xl/workbook.xml'))
    rel_id = next((sheet.get(f'{_DOC_REL}id') for sheet in workbook.iterfind('.//main:sheet', _NAMESPACES) if sheet.get('name') == sheet_name), None)
    if not rel_id:
        return []

    def relationships(part: str) -> dict[str, str]:
        rel_path = posixpath.join(posixpath.dirname(part), '_rels', f'{posixpath.basename(part)}.rels')
        if rel_path not in archive.namelist():
            return {}
        root = ElementTree.fromstring(archive.read(rel_path))
        return {
            item.get('Id'): posixpath.normpath(posixpath.join(posixpath.dirname(part), item.get('Target'))).lstrip('/')
            if not item.get('Target', '').startswith('/') else item.get('Target').lstrip('/')
            for item in root.iterfind('rel:Relationship', _NAMESPACES)
        }

    sheet_part = relationships('xl/workbook.xml').get(rel_id)
    if not sheet_part:
        return []
    pictures = []
    for target in relationships(sheet_part).values():
        if '/drawings/' not in f'/{target}' or not target.endswith('.xml') or target not in archive.namelist():
            continue
        media = relationships(target)
        for anchor in ElementTree.fromstring(archive.read(target)):
            start = anchor.find('xdr:from', _NAMESPACES)
            blip = anchor.find('.//a:blip', _NAMESPACES)
            if start is None or blip is None:
                continue
            path = media.get(blip.get(f'{_DOC_REL}embed'))
            if path and path in archive.namelist():
                pictures.append((int(start.findtext('xdr:row', '0', _NAMESPACES)), int(start.findtext('xdr:col', '0', _NAMESPACES)), path))
    return pictures


def _risk_labels(frame: pd.DataFrame) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Locate the sheet's 'Scheme Riskometer' and 'Benchmark Riskometer' labels."""
    schemes, benchmarks = [], []
    for row_index, row in enumerate(frame.itertuples(index=False, name=None)):
        for column_index, cell in enumerate(row):
            if pd.isna(cell) or len(str(cell)) > 90:
                continue
            text = str(cell)
            if re.search(rf'(?i)benchmark.{{0,14}}{_RISK_WORD}', text):
                benchmarks.append((row_index, column_index))
            elif re.search(rf'(?i)scheme.{{0,14}}{_RISK_WORD}', text):
                schemes.append((row_index, column_index))
    return schemes, benchmarks


def read_scheme_riskometer(payload: bytes, sheet_name: str, frame: pd.DataFrame) -> str | None:
    """Find the scheme's own riskometer picture in a workbook and read its level."""
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            schemes, benchmarks = _risk_labels(frame)
            pictures = _sheet_pictures(archive, sheet_name)
            if not schemes or not pictures:
                return None

            def distance(picture: tuple[int, int, str], label: tuple[int, int]) -> int:
                return abs(picture[0] - label[0]) + abs(picture[1] - label[1])

            label = schemes[0]
            # Never read the benchmark's picture by mistake.
            candidates = [
                picture for picture in pictures
                if all(distance(picture, label) < distance(picture, benchmark) for benchmark in benchmarks)
            ]
            ranked = sorted(candidates, key=lambda picture: distance(picture, label))
            if not ranked or (len(ranked) > 1 and distance(ranked[1], label) == distance(ranked[0], label)):
                return None
            return read_risk_level(archive.read(ranked[0][2]))
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError) as exc:
        logger.warning('Riskometer could not be read from sheet %s: %s', sheet_name, exc)
        return None
