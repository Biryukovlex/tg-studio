import { readFileSync } from 'node:fs';
import { afterEach, expect, test, vi } from 'vitest';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

test('provider switch and model refresh use the same accessible dropdown', async () => {
  document.body.innerHTML = `<select data-ui-dropdown id="studio-provider"><option value="openrouter">OpenRouter</option><option value="ollama">Ollama</option></select>
    <div data-provider-panel="openrouter"><input name="model"></div>
    <div data-provider-panel="ollama" hidden><input id="ollama-url" value="http://127.0.0.1:11434">
      <select data-ui-dropdown id="ollama-model"><option value="">Load installed models</option></select><button data-load-models="ollama">Refresh models</button></div>
    <p data-model-connection-error hidden></p>`;
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ok:true,json:async()=>({models:[{id:'synthetic-local',name:'Synthetic local model'}]})}));
  window.eval(readFileSync('../app/web/static/ui-controls.js','utf8'));
  window.eval(readFileSync('../app/web/static/provider-connections.js','utf8'));
  document.dispatchEvent(new Event('DOMContentLoaded'));
  const provider = document.querySelector<HTMLSelectElement>('#studio-provider')!;
  provider.value = 'ollama';
  provider.dispatchEvent(new Event('change'));
  expect(document.querySelector<HTMLInputElement>('[name="model"]')!.disabled).toBe(true);
  expect(document.querySelector<HTMLInputElement>('#ollama-url')!.disabled).toBe(false);
  document.querySelector<HTMLButtonElement>('[data-load-models]')!.click();
  await vi.waitFor(() => expect(document.querySelector<HTMLSelectElement>('#ollama-model')!.value).toBe('synthetic-local'));
  const native = document.querySelector('#ollama-model')!;
  const trigger = native.parentElement!.querySelector<HTMLButtonElement>('button[aria-haspopup="listbox"]')!;
  expect(trigger.textContent).toContain('Synthetic local model');
  trigger.click();
  const options = [...native.parentElement!.querySelectorAll('[role="option"]')];
  expect(options.map(option=>option.textContent)).toEqual(['Synthetic local model']);
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('ollama_url=http%3A%2F%2F127.0.0.1%3A11434'), expect.any(Object));
});
