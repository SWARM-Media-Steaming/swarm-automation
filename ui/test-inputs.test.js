const test = require("node:test");
const assert = require("node:assert/strict");
const { controlModel } = require("./test-inputs.js");

test("maps every schema input type to a usable control", () => {
  const expected = {
    text: ["input", "text", false], environment: ["input", "text", false],
    number: ["input", "number", false], boolean: ["input", "checkbox", false],
    file: ["input", "text", true], directory: ["input", "text", true],
    select: ["select", "text", false], device: ["select", "text", false],
    secret: ["input", "password", false],
  };
  for (const [inputType, shape] of Object.entries(expected)) {
    const model = controlModel({ inputType });
    assert.deepEqual([model.element, model.inputType, model.picker], shape);
  }
});

test("renders provenance and never returns a secret value", () => {
  assert.equal(controlModel({ inputType: "text", state: "detected" }).stateLabel, "Detected");
  assert.equal(controlModel({ inputType: "text", state: "saved" }).stateLabel, "Using saved value");
  assert.equal(controlModel({ inputType: "text", state: "required" }).stateLabel, "Required");
  assert.equal(controlModel({ inputType: "text", state: "invalid" }).stateLabel, "Invalid");
  const secret = controlModel({ inputType: "secret", value: "do-not-render", hasValue: true });
  assert.equal(secret.value, "");
  assert.match(secret.placeholder, /keychain/);
});
