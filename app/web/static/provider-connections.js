(function () {
  "use strict";
  function init() {
    var picker = document.getElementById("studio-provider");
    if (!picker) return;
    function update() {
      document.querySelectorAll("[data-provider-panel]").forEach(function (panel) {
        panel.hidden = panel.dataset.providerPanel !== picker.value;
        panel.querySelectorAll("input, select").forEach(function (input) { input.disabled = panel.hidden; input.dispatchEvent(new Event("change")); });
      });
    }
    picker.addEventListener("change", update);
    update();
    document.querySelectorAll("[data-load-models]").forEach(function (button) {
      button.addEventListener("click", async function () {
        var provider = button.dataset.loadModels;
        var select = document.getElementById(provider === "ollama" ? "ollama-model" : "openai-model");
        var error = document.querySelector("[data-model-connection-error]");
        var selected = select.value;
        button.disabled = true;
        error.hidden = true;
        try {
          var response = await fetch("/settings/providers/models?provider=" + encodeURIComponent(provider) + (provider === "ollama" ? "&ollama_url=" + encodeURIComponent(document.getElementById("ollama-url").value) : ""), { credentials: "same-origin" });
          var payload = await response.json();
          if (!response.ok) throw new Error(payload.error && payload.error.message || "Models could not load.");
          if (!payload.models.length) throw new Error("No models available. Install an Ollama model or check ChatGPT access.");
          select.replaceChildren();
          payload.models.forEach(function (model) {
            var option = document.createElement("option");
            option.value = model.id;
            option.textContent = model.name;
            option.selected = model.id === selected;
            select.appendChild(option);
          });
          select.dispatchEvent(new Event("tg:options"));
          select.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (reason) {
          error.textContent = reason.message || "Models could not load.";
          error.hidden = false;
        } finally { button.disabled = false; }
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}());
