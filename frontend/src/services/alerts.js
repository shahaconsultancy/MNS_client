import Swal from 'sweetalert2';
import 'sweetalert2/dist/sweetalert2.min.css';
import './alerts.css';

// One look for every popup in the app: the site's font, navy buttons, red for destructive actions.
const base = Swal.mixin({
  customClass: { popup: 'mns-swal', title: 'mns-swal__title', htmlContainer: 'mns-swal__text', confirmButton: 'mns-swal__btn', cancelButton: 'mns-swal__btn mns-swal__btn--cancel' },
  buttonsStyling: false,
  reverseButtons: true,
});

export function alertSuccess(title, text = '') {
  return base.fire({ icon: 'success', title, text, confirmButtonText: 'OK', timer: 2500, timerProgressBar: true });
}

export function alertError(title, text = '') {
  return base.fire({ icon: 'error', title, text, confirmButtonText: 'OK' });
}

// Asks before a client is deleted, and runs the delete from the popup so its spinner and any error stay there.
// Resolves true only once action() has succeeded.
export async function confirmDeleteClient(clientName, action) {
  const result = await base.fire({
    icon: 'warning',
    title: 'Delete Client Permanently?',
    html: `
      <p class="mns-swal__client">${escapeHtml(clientName)}</p>
      <p>You are about to remove this client and all their associated financial information from the system.</p>
      <p>This includes personal details, financial records, investments, insurance, goals, and retirement plans.</p>
      <p class="mns-swal__warn">Once deleted, this data cannot be recovered.</p>
      <p class="mns-swal__ask">Are you sure you want to continue?</p>`,
    showCancelButton: true,
    confirmButtonText: 'Yes, Delete Permanently',
    cancelButtonText: 'No, Keep Client',
    customClass: { popup: 'mns-swal', title: 'mns-swal__title', htmlContainer: 'mns-swal__text', confirmButton: 'mns-swal__btn mns-swal__btn--danger', cancelButton: 'mns-swal__btn mns-swal__btn--cancel' },
    // The safe choice has focus, so Enter never deletes by accident.
    focusCancel: true,
    showLoaderOnConfirm: true,
    allowOutsideClick: () => !Swal.isLoading(),
    allowEscapeKey: () => !Swal.isLoading(),
    preConfirm: async () => {
      try {
        await action();
        return true;
      } catch (err) {
        Swal.showValidationMessage(err.message || 'The client could not be deleted. Please try again.');
        return false;
      }
    },
  });
  return Boolean(result.isConfirmed && result.value);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
}

