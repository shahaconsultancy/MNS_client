from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

URL = '/api/auth/reset-password/'


@override_settings(PASSWORD_RESET_KEY='Apple@1990')
class ResetPasswordTests(APITestCase):
    def setUp(self):
        cache.clear()  # the throttle counts live in the cache
        self.user = get_user_model().objects.create_user(email='adv@x.com', password='OldPass#2026')

    def body(self, **over):
        data = {'email': 'adv@x.com', 'key': 'Apple@1990', 'new_password': 'Fresh#Pass2026', 'confirm_password': 'Fresh#Pass2026'}
        data.update(over)
        return data

    def test_right_key_changes_the_password(self):
        res = self.client.post(URL, self.body(email='ADV@x.com'), format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Fresh#Pass2026'))
        login = self.client.post('/api/auth/login/', {'email': 'adv@x.com', 'password': 'Fresh#Pass2026'}, format='json')
        self.assertEqual(login.status_code, 200)

    def test_wrong_key_changes_nothing(self):
        res = self.client.post(URL, self.body(key='apple@1990'), format='json')
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('OldPass#2026'))

    def test_unknown_email_gets_the_same_message_as_a_wrong_key(self):
        wrong_key = self.client.post(URL, self.body(key='nope'), format='json').json()
        unknown = self.client.post(URL, self.body(email='who@x.com'), format='json').json()
        self.assertEqual(wrong_key, unknown)

    def test_passwords_must_match(self):
        res = self.client.post(URL, self.body(confirm_password='Different#1'), format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('confirm_password', res.json())

    def test_weak_password_is_rejected(self):
        res = self.client.post(URL, self.body(new_password='12345678', confirm_password='12345678'), format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('new_password', res.json())

    @override_settings(PASSWORD_RESET_KEY='')
    def test_switched_off_without_a_key(self):
        res = self.client.post(URL, self.body(key=''), format='json')
        self.assertEqual(res.status_code, 400)


class LoginLimitTests(APITestCase):
    def setUp(self):
        cache.clear()
        get_user_model().objects.create_user(email='adv@x.com', password='RightPass#2026')

    def login(self, password, email='adv@x.com'):
        return self.client.post('/api/auth/login/', {'email': email, 'password': password}, format='json')

    def test_locked_after_three_wrong_passwords(self):
        for _ in range(3):
            self.assertEqual(self.login('wrong').status_code, 403)
        locked = self.login('RightPass#2026')  # even the right password is refused while locked
        self.assertEqual(locked.status_code, 429)
        self.assertIn('Too many failed attempts', locked.json()['detail'])

    def test_two_wrong_then_right_signs_in_and_resets_the_count(self):
        self.login('wrong')
        self.login('wrong')
        self.assertEqual(self.login('RightPass#2026').status_code, 200)
        for _ in range(3):
            self.assertEqual(self.login('wrong').status_code, 403)

    def test_another_email_is_not_locked(self):
        for _ in range(3):
            self.login('wrong')
        self.assertEqual(self.login('x', email='other@x.com').status_code, 403)
