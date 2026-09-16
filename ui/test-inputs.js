(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SwarmTestInputs = api;
})(typeof window === "undefined" ? globalThis : window, () => {
  "use strict";

  function controlModel(input) {
    const type = input.inputType || "text";
    return {
      element: ["select", "device"].includes(type) ? "select" : "input",
      inputType: type === "secret" ? "password" : type === "number" ? "number" : type === "boolean" ? "checkbox" : "text",
      stateLabel: input.state === "saved" ? "Using saved value"
        : input.state === "detected" ? "Detected"
          : input.state === "required" ? "Required"
            : input.state === "invalid" ? "Invalid" : "Ready",
      picker: ["file", "directory"].includes(type),
      value: type === "secret" ? "" : (input.value || ""),
      placeholder: type === "secret" && input.hasValue ? "Saved securely in OS keychain" : "",
    };
  }

  return { controlModel };
});
