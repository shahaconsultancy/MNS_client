const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateLogin({ email, password }) {
  const errors = {};
  if (!email.trim()) errors.email = 'Email is required.';
  else if (!EMAIL_REGEX.test(email.trim())) errors.email = 'Enter a valid email address.';
  if (!password) errors.password = 'Password is required.';
  return errors;
}

export function validateReset({ email, key, newPassword, confirmPassword }) {
  const errors = {};
  if (!email.trim()) errors.form = 'Enter your email in the sign-in form first.';
  else if (!EMAIL_REGEX.test(email.trim())) errors.form = 'Enter a valid email address in the sign-in form first.';
  if (!key) errors.key = 'Enter the secret key.';
  if (!newPassword) errors.newPassword = 'Enter a new password.';
  else if (newPassword.length < 8) errors.newPassword = 'Use at least 8 characters.';
  if (!confirmPassword) errors.confirmPassword = 'Confirm the new password.';
  else if (confirmPassword !== newPassword) errors.confirmPassword = 'The two passwords do not match.';
  return errors;
}
