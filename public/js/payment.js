/** Optional live Razorpay integration. The demo gateway uses ordinary server forms. */
(() => {
  'use strict';
  const button = document.getElementById('razorpay-button');
  const configNode = document.getElementById('payment-config');
  const form = document.getElementById('payment-verification');
  const feedback = document.getElementById('payment-feedback');
  if (!button || !configNode || !form) return;

  /** Open provider checkout and submit its signed response for server verification. */
  button.addEventListener('click', () => {
    if (!window.Razorpay) {
      feedback.textContent = 'The payment provider did not load. Please refresh or contact support.';
      return;
    }
    const config = JSON.parse(configNode.textContent);
    config.handler = (result) => {
      for (const name of ['razorpay_order_id', 'razorpay_payment_id', 'razorpay_signature']) {
        form.elements[name].value = result[name] || '';
      }
      button.disabled = true;
      button.textContent = 'Verifying payment…';
      form.submit();
    };
    config.modal = { ondismiss() { feedback.textContent = 'Checkout closed. Your order is awaiting payment.'; } };
    const gateway = new window.Razorpay(config);
    gateway.on('payment.failed', () => { feedback.textContent = 'Payment was not completed. You can retry within the reservation window.'; });
    gateway.open();
  });
})();
