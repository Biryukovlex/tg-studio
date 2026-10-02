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

  function activateSaveConfirmation(saveConfirmation) {
    if (!saveConfirmation) return;
    const section = saveConfirmation.dataset.section;
    const target = section ? document.getElementById(section) : null;
    const savedButton = target?.querySelector('.form-actions button[type="submit"]');
    const originalButtonText = savedButton?.textContent;
    let dismissed = false;

    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      saveConfirmation.classList.add('is-leaving');
      window.setTimeout(() => saveConfirmation.remove(), reducedMotion ? 0 : 360);
    };

    requestAnimationFrame(() => {
      saveConfirmation.classList.add('is-visible');
      if (savedButton) {
        savedButton.textContent = 'Saved';
        savedButton.classList.add('is-saved');
      }
    });

    window.setTimeout(() => {
      if (savedButton && originalButtonText) {
        savedButton.textContent = originalButtonText;
        savedButton.classList.remove('is-saved');
      }
    }, reducedMotion ? 0 : 1900);
    window.setTimeout(dismiss, reducedMotion ? 4000 : 4200);
    saveConfirmation.querySelector('.save-confirmation-close')?.addEventListener('click', dismiss);
  }

  activateSaveConfirmation(document.querySelector('[data-save-confirmation]'));

  // Settings saves (T45): POST the form, accept the canonical T51 JSON
  // response and merge it in place. The panel node stays mounted, so there
  // is no remount blink and scroll/focus never move. Edits are preserved on
  // validation, conflict and network failures.
  const dirtyForms = new Set();

  function settingsFeedback(form) {
    return form.querySelector('[data-save-feedback]');
  }

  function markDirty(form) {
    const section = form.dataset.settingsSave;
    if (!section || form.dataset.submitting === 'true') return;
    dirtyForms.add(section);
    const note = settingsFeedback(form);
    if (note) {
      note.textContent = 'Unsaved changes';
      note.classList.add('is-dirty');
    }
  }

  function markClean(form) {
    dirtyForms.delete(form.dataset.settingsSave);
    const note = settingsFeedback(form);
    if (note) {
      note.textContent = '';
      note.classList.remove('is-dirty');
    }
  }

  function showSettingsNotice(message) {
    const current = document.querySelector('[data-save-confirmation]');
    current?.remove();
    const notice = document.createElement('aside');
    notice.className = 'save-confirmation';
    notice.dataset.saveConfirmation = '';
    notice.setAttribute('role', 'status');
    notice.setAttribute('aria-live', 'polite');
    const copy = document.createElement('span');
    copy.className = 'save-confirmation-copy';
    const strong = document.createElement('strong');
    strong.textContent = 'Saved';
    const text = document.createElement('span');
    text.textContent = message;
    copy.append(strong, text);
    const close = document.createElement('button');
    close.className = 'save-confirmation-close';
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss confirmation');
    close.textContent = '×';
    const progress = document.createElement('span');
    progress.className = 'save-confirmation-progress';
    progress.setAttribute('aria-hidden', 'true');
    notice.append(copy, close, progress);
    document.getElementById('main-content')?.prepend(notice);
    activateSaveConfirmation(notice);
  }

  function provenanceLabel(entry) {
    if (!entry || typeof entry !== 'object') return { text: 'default', saved: false };
    if (entry.source === 'db') {
      const when = typeof entry.updated_at === 'string' && entry.updated_at ? entry.updated_at.slice(0, 16) : '';
      return { text: when ? `Saved ${when} UTC` : 'Saved', saved: true };
    }
    if (entry.source === 'env') return { text: 'from .env', saved: false };
    return { text: 'default', saved: false };
  }

  function mergeProvenance(form, fields) {
    if (!fields || typeof fields !== 'object') return;
    form.querySelectorAll('[data-provenance-for]').forEach((slot) => {
      const key = slot.dataset.provenanceFor;
      const label = provenanceLabel(fields[key]);
      const tag = slot.querySelector('.source-tag');
      if (tag) {
        tag.textContent = label.text;
        tag.classList.toggle('saved', label.saved);
      }
      slot.querySelectorAll('.link-btn[form]').forEach((reset) => {
        reset.hidden = !label.saved;
      });
    });
  }

  function mergeSetupState(payload) {
    const setup = payload.setup;
    if (!setup || typeof setup !== 'object') return;
    document.querySelectorAll('[data-setup-readiness]').forEach((pill) => {
      pill.classList.toggle('ok', setup.ready === true);
      const blockers = Array.isArray(setup.blockers) ? setup.blockers : [];
      const label = setup.ready === true ? 'Ready' : (blockers[0]?.message || 'Not ready');
      pill.textContent = label;
    });
    const restart = payload.restart_required === true;
    const notices = [...document.querySelectorAll('[data-restart-notice]')];
    let banner = notices.find((node) => !node.hasAttribute('data-idle')) || null;
    let idle = notices.find((node) => node.hasAttribute('data-idle')) || null;
    const telegramPanel = document.getElementById('telegram');
    if (restart && !banner && telegramPanel) {
      banner = document.createElement('div');
      banner.className = 'banner warn restart-banner';
      banner.dataset.restartNotice = '';
      banner.setAttribute('role', 'status');
      banner.textContent = 'Connection saved. Restart the collector to use the updated credentials.';
      const connectionForm = telegramPanel.querySelector('form[data-settings-save]');
      if (connectionForm) connectionForm.before(banner);
      else telegramPanel.prepend(banner);
    }
    if (!restart && !idle && telegramPanel) {
      idle = document.createElement('p');
      idle.className = 'panel-note restart-hint';
      idle.dataset.restartNotice = '';
      idle.dataset.idle = '';
      idle.textContent = 'Connection changes take effect after the collector restarts.';
      const connectionForm = telegramPanel.querySelector('form[data-settings-save]');
      if (connectionForm) connectionForm.before(idle);
      else telegramPanel.prepend(idle);
    }
    if (banner) banner.hidden = !restart;
    if (idle) idle.hidden = restart;
    const connection = payload.connection;
    if (connection && typeof connection === 'object') {
      const statusLine = document.querySelector('[data-connection-status]');
      const apiId = document.querySelector('[data-connection-api-id]');
      if (apiId) apiId.textContent = connection.api_id != null ? String(connection.api_id) : '—';
      const sessionPill = statusLine?.querySelector('.pill');
      if (sessionPill && typeof connection.has_session === 'boolean') {
        sessionPill.classList.toggle('ok', connection.has_session);
        const icon = sessionPill.querySelector('i');
        sessionPill.textContent = connection.has_session ? 'Session set' : 'Not set';
        if (icon) sessionPill.prepend(icon);
      }
    }
    const keyEntry = payload.fields?.['studio.openrouter_api_key'];
    if (keyEntry && typeof keyEntry.set === 'boolean') {
      document.querySelectorAll('[data-key-status]').forEach((node) => {
        node.textContent = '';
        const pill = document.createElement('span');
        pill.className = keyEntry.set ? 'pill ok' : 'pill';
        pill.textContent = keyEntry.set ? 'Set' : 'Not set';
        node.append(pill);
      });
    }
    const researchEnabled = payload.fields?.['research.enabled'];
    if (researchEnabled && typeof researchEnabled.value === 'boolean') {
      document.querySelectorAll('[data-switch-text]').forEach((node) => {
        node.textContent = researchEnabled.value ? 'Enabled' : 'Disabled';
      });
    }
  }

  function clearFieldErrors(form) {
    form.querySelectorAll('.field-error[data-settings-error]').forEach((node) => node.remove());
    form.querySelectorAll('[aria-invalid="true"]').forEach((node) => node.removeAttribute('aria-invalid'));
    form.querySelectorAll('.field.has-error').forEach((node) => {
      if (!node.querySelector('.field-error')) node.classList.remove('has-error');
    });
  }

  function showFieldErrors(form, errors) {
    let firstInvalid = null;
    Object.entries(errors).forEach(([field, message]) => {
      const input = form.querySelector(`[name="${field}"]`);
      if (!(input instanceof HTMLElement)) return;
      input.setAttribute('aria-invalid', 'true');
      const control = input.closest('.field-control') || input.parentElement;
      const error = document.createElement('p');
      error.className = 'field-error';
      error.dataset.settingsError = '';
      error.textContent = String(message);
      control?.append(error);
      input.closest('.field')?.classList.add('has-error');
      firstInvalid ??= input;
    });
    firstInvalid?.focus({ preventScroll: true });
  }

  function showPanelError(panel, message) {
    panel.querySelector('[data-settings-save-error]')?.remove();
    const banner = document.createElement('div');
    banner.className = 'banner warn';
    banner.dataset.settingsSaveError = '';
    banner.setAttribute('role', 'alert');
    banner.textContent = message;
    panel.prepend(banner);
  }

  const settingsStack = document.querySelector('.settings-stack');
  if (settingsStack) {
    settingsStack.addEventListener('input', (event) => {
      const form = event.target.closest?.('form[data-settings-save]');
      if (form instanceof HTMLFormElement) markDirty(form);
    });

    settingsStack.addEventListener('submit', async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
      if (!form.hasAttribute('data-settings-save')) return;

      const action = new URL(form.action, window.location.href);
      if (action.origin !== window.location.origin || !action.pathname.startsWith('/settings/')) return;

      event.preventDefault();
      const panel = form.closest('.panel');
      if (!panel?.id || form.dataset.submitting === 'true') return;

      form.dataset.submitting = 'true';
      const submitter = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
      if (submitter) {
        submitter.disabled = true;
        submitter.classList.add('is-busy');
      }
      form.querySelector('[data-settings-save-error]')?.remove();

      try {
        const response = await fetch(action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
          headers: { Accept: 'application/json' },
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch {
          payload = null;
        }
        if (!response.ok || !payload || payload.ok !== true) {
          const errors = payload && typeof payload.errors === 'object' ? payload.errors : null;
          clearFieldErrors(form);
          if (errors && Object.keys(errors).length > 0) {
            showFieldErrors(form, errors);
          } else {
            const message = payload?.error?.message || 'Could not save these settings. Check the connection and try again.';
            showPanelError(panel, message);
          }
          return;
        }

        clearFieldErrors(form);
        mergeProvenance(form, payload.fields);
        mergeSetupState(payload);
        // Secrets are write-only: blank keeps the stored value, so clear the
        // inputs after a save instead of echoing anything back.
        form.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
        const clearKey = form.querySelector('input[name="clear_openrouter_api_key"]');
        if (clearKey instanceof HTMLInputElement) clearKey.checked = false;
        markClean(form);
        showSettingsNotice(payload.notice || 'Settings saved.');
      } catch {
        showPanelError(panel, 'Could not save these settings. Check the connection and try again.');
      } finally {
        form.dataset.submitting = 'false';
        if (submitter) {
          submitter.disabled = false;
          submitter.classList.remove('is-busy');
        }
      }
    });

    // Reset buttons post their hidden form to /settings/*/reset. They merge
    // through the same canonical response, and refresh the field input from
    // the server value because a reset changes what the user sees.
    const RESET_FIELD_NAMES = {
      'collection.poll_minutes': 'poll_minutes',
      'collection.track_days': 'track_days',
      'collection.backfill_limit': 'backfill_limit',
      'studio.model': 'model',
      'research.enabled': 'research_enabled',
      'research.blocked_domains': 'blocked_domains',
    };
    settingsStack.addEventListener('submit', (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
      if (form.hasAttribute('data-settings-save')) return;
      const action = new URL(form.action, window.location.href);
      if (!action.pathname.endsWith('/reset')) return;
      event.preventDefault();
      const opener = document.querySelector(`form[data-settings-save] [form="${form.id}"]`);
      const owner = opener?.closest('form[data-settings-save]');
      if (!(owner instanceof HTMLFormElement) || owner.dataset.submitting === 'true') return;
      owner.dataset.submitting = 'true';
      fetch(action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      }).then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (response.ok && payload?.ok === true) {
          mergeProvenance(owner, payload.fields);
          mergeSetupState(payload);
          const fields = payload.fields || {};
          Object.entries(RESET_FIELD_NAMES).forEach(([key, name]) => {
            const entry = fields[key];
            const input = owner.querySelector(`[name="${name}"]`);
            if (!entry || !(input instanceof HTMLElement) || !('value' in entry)) return;
            if (input instanceof HTMLInputElement && input.type === 'checkbox') {
              input.checked = entry.value === true;
            } else if (input instanceof HTMLElement && 'value' in input) {
              input.value = String(entry.value ?? '');
            }
          });
          markClean(owner);
          showSettingsNotice(payload.notice || 'Setting reset.');
        } else {
          const panel = owner.closest('.panel');
          if (panel) showPanelError(panel, payload?.error?.message || 'Could not reset this setting.');
        }
      }).catch(() => {
        const panel = owner.closest('.panel');
        if (panel) showPanelError(panel, 'Could not reset this setting. Check the connection and try again.');
      }).finally(() => {
        owner.dataset.submitting = 'false';
      });
    });
  }

  // Unsaved guard: leaving Configuration for Agent logs (or the page) with
  // unsaved edits asks first; saved work is never at risk.
  document.querySelectorAll('[data-settings-view-tabs] a').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (dirtyForms.size === 0) return;
      const stay = !window.confirm('Leave unsaved settings? Your unsaved edits will be lost. Saved settings are unchanged.');
      if (stay) event.preventDefault();
      else dirtyForms.clear();
    });
  });
  window.addEventListener('beforeunload', (event) => {
    if (dirtyForms.size === 0) return;
    event.preventDefault();
  });

  // Section navigation: arrows/Home/End move between section links without
  // changing the page; Enter follows the link natively.
  document.querySelectorAll('[data-settings-section-nav]').forEach((nav) => {
    const links = () => [...nav.querySelectorAll('a')];
    nav.addEventListener('keydown', (event) => {
      const items = links();
      const current = items.indexOf(document.activeElement);
      if (current === -1) return;
      let next = -1;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % items.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + items.length) % items.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = items.length - 1;
      if (next === -1) return;
      event.preventDefault();
      items[next].focus();
    });
  });

  // Agent logs: the list, filters and pagination are server-rendered over all
  // stored results. This only checks storage availability so an outage reads
  // as unavailable instead of empty.
  const logsAvailability = document.querySelector('[data-logs-availability]');
  if (logsAvailability) {
    fetch(window.location.href, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        const unavailable = !response.ok || payload?.available === false;
        if (unavailable) {
          const message = payload?.error?.message || 'Agent log storage is unavailable. Saved results cannot be listed right now; try again later.';
          logsAvailability.textContent = message;
          logsAvailability.hidden = false;
        } else {
          logsAvailability.hidden = true;
        }
      })
      .catch(() => { /* Keep the server-rendered list; do not invent an outage. */ });
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-delete-dialog-open]');
    if (button instanceof HTMLButtonElement) {
      const dialog = document.getElementById(button.dataset.deleteDialogOpen || '');
      if (!(dialog instanceof HTMLDialogElement)) return;
      dialog.showModal();
      dialog.querySelector('input[name="confirmation"]')?.focus();
      return;
    }

    const closeButton = event.target.closest('[data-delete-dialog-close]');
    if (closeButton instanceof HTMLButtonElement) {
      closeButton.closest('dialog')?.close();
      return;
    }

    if (event.target instanceof HTMLDialogElement && event.target.matches('.delete-channel-dialog')) {
      event.target.close();
    }
  });

  // Charts — read data from data-chart attributes to keep CSP script-src 'self'.
  function parseChartData(element) {
    const raw = element.getAttribute('data-chart') || element.dataset.chart;
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  const trendCanvas = document.getElementById('trendChart');
  if (trendCanvas && typeof window.Chart !== 'undefined') {
    const data = parseChartData(trendCanvas);
    if (data && Array.isArray(data.labels)) {
      const motionOK = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const styles = getComputedStyle(document.documentElement);
      const chartColors = {
        reach: styles.getPropertyValue('--cyan').trim(),
        reactions: styles.getPropertyValue('--coral').trim(),
        comments: styles.getPropertyValue('--teal').trim(),
        shares: styles.getPropertyValue('--blue').trim(),
        muted: styles.getPropertyValue('--muted').trim(),
        grid: styles.getPropertyValue('--chart-grid').trim(),
      };
      const trendChart = new window.Chart(trendCanvas, {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [
            { label: 'Reach / Views', data: data.views, borderColor: chartColors.reach, backgroundColor: 'rgba(26, 211, 238, .10)', fill: true, tension: 0.36, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 },
            { label: 'Reactions', data: data.reactions, borderColor: chartColors.reactions, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Comments', data: data.comments, borderColor: chartColors.comments, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Shares', data: data.shares, borderColor: chartColors.shares, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          animation: motionOK ? { duration: 1400, easing: 'easeOutQuart' } : false,
          plugins: {
            legend: { position: 'top', align: 'start', labels: { color: chartColors.muted, boxWidth: 24, boxHeight: 2, padding: 24, usePointStyle: false, font: { family: 'Manrope Variable', size: 12, weight: 550 } } },
            tooltip: { backgroundColor: '#0d1a27', borderColor: 'rgba(119, 151, 177, .3)', borderWidth: 1, titleColor: '#f4f8fb', bodyColor: '#a6b5c3', padding: 12, displayColors: true },
          },
          scales: {
            x: { ticks: { color: chartColors.muted, maxTicksLimit: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { display: false }, border: { display: false } },
            y: { beginAtZero: true, ticks: { color: chartColors.muted, padding: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { color: chartColors.grid, borderDash: [4, 5] }, border: { display: false } },
          },
        },
      });

      const rawSeries = {
        labels: [...data.labels],
        values: [data.views, data.reactions, data.comments, data.shares].map((s) => [...s]),
      };
      function groupedIndexes(period) {
        if (period === 'day') return rawSeries.labels.map((_, i) => i);
        const lastByPeriod = new Map();
        rawSeries.labels.forEach((label, index) => {
          const date = new Date(`${label}T00:00:00Z`);
          let key;
          if (period === 'month') {
            key = label.slice(0, 7);
          } else {
            const mondayOffset = (date.getUTCDay() + 6) % 7;
            date.setUTCDate(date.getUTCDate() - mondayOffset);
            key = date.toISOString().slice(0, 10);
          }
          lastByPeriod.set(key, index);
        });
        return [...lastByPeriod.values()];
      }
      document.querySelectorAll('[data-chart-period]').forEach((button) => {
        button.addEventListener('click', () => {
          const indexes = groupedIndexes(button.dataset.chartPeriod);
          trendChart.data.labels = indexes.map((i) => rawSeries.labels[i]);
          trendChart.data.datasets.forEach((dataset, si) => {
            dataset.data = indexes.map((i) => rawSeries.values[si][i]);
          });
          document.querySelectorAll('[data-chart-period]').forEach((item) => {
            const selected = item === button;
            item.classList.toggle('active', selected);
            item.setAttribute('aria-pressed', String(selected));
          });
          trendChart.update(motionOK ? undefined : 'none');
        });
      });
    }
  }

  const postCanvas = document.getElementById('postChart');
  if (postCanvas && typeof window.Chart !== 'undefined') {
    const data = parseChartData(postCanvas);
    if (data && Array.isArray(data.labels)) {
      const motionOK = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const styles = getComputedStyle(document.documentElement);
      const colors = {
        reach: styles.getPropertyValue('--cyan').trim(),
        reactions: styles.getPropertyValue('--coral').trim(),
        comments: styles.getPropertyValue('--teal').trim(),
        shares: styles.getPropertyValue('--blue').trim(),
        muted: styles.getPropertyValue('--muted').trim(),
        grid: styles.getPropertyValue('--chart-grid').trim(),
      };
      new window.Chart(postCanvas, {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [
            { label: 'Reach / Views', data: data.views, borderColor: colors.reach, backgroundColor: 'rgba(25, 210, 236, .10)', fill: true, tension: 0.36, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 },
            { label: 'Reactions', data: data.reactions, borderColor: colors.reactions, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Comments', data: data.comments, borderColor: colors.comments, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Shares', data: data.shares, borderColor: colors.shares, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          animation: motionOK ? { duration: 1300, easing: 'easeOutQuart' } : false,
          plugins: {
            legend: { position: 'top', align: 'start', labels: { color: colors.muted, boxWidth: 24, boxHeight: 2, padding: 24, font: { family: 'Manrope Variable', size: 12, weight: 550 } } },
            tooltip: { backgroundColor: '#0d1a27', borderColor: 'rgba(119, 151, 177, .3)', borderWidth: 1, titleColor: '#f4f8fb', bodyColor: '#a6b5c3', padding: 12 },
          },
          scales: {
            x: { ticks: { color: colors.muted, maxTicksLimit: 10, font: { family: 'Manrope Variable', size: 11 } }, grid: { display: false }, border: { display: false } },
            y: { beginAtZero: true, ticks: { color: colors.muted, padding: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { color: colors.grid, borderDash: [4, 5] }, border: { display: false } },
          },
        },
      });
    }
  }
})();
