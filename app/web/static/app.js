(() => {
  document.documentElement.classList.add('motion-ready');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const format = new Intl.NumberFormat('en-US');

  function animateNumber(element) {
    const finalValue = Number(element.dataset.count || 0);
    if (!Number.isFinite(finalValue) || reducedMotion) {
      element.textContent = format.format(finalValue);
      return;
    }

    const duration = 850;
    const start = performance.now();
    const easeOut = (value) => 1 - Math.pow(1 - value, 4);
    element.textContent = '0';

    function frame(now) {
      const progress = Math.min((now - start) / duration, 1);
      element.textContent = format.format(Math.round(finalValue * easeOut(progress)));
      if (progress < 1) requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  document.querySelectorAll('[data-count]').forEach(animateNumber);

  const revealItems = document.querySelectorAll('[data-reveal]');
  if (reducedMotion || !('IntersectionObserver' in window)) {
    revealItems.forEach((item) => item.classList.add('is-visible'));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.08 });
    revealItems.forEach((item) => observer.observe(item));
  }

  document.querySelectorAll('[data-auto-submit] select').forEach((select) => {
    select.addEventListener('change', () => {
      document.body.classList.add('is-navigating');
      select.form.requestSubmit();
    });
  });

  document.querySelectorAll('.refresh-form').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button');
      button?.classList.add('is-busy');
      if (button) button.disabled = true;
    });
  });
})();
