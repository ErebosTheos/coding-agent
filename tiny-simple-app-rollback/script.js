function add(a, b) {
  return a + b;
}

function subtract(a, b) {
  return a - b;
}

function multiply(a, b) {
  return a * b;
}

function divide(a, b) {
  return a / b;
}

function toNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

if (typeof document !== "undefined") {
  const firstNumberInput = document.getElementById("first-number");
  const secondNumberInput = document.getElementById("second-number");
  const addButton = document.getElementById("add-btn");
  const subtractButton = document.getElementById("subtract-btn");
  const multiplyButton = document.getElementById("multiply-btn");
  const divideButton = document.getElementById("divide-btn");
  const resultOutput = document.getElementById("result");

  function updateResult(operation) {
    if (!firstNumberInput || !secondNumberInput || !resultOutput) {
      return;
    }

    const firstNumber = toNumber(firstNumberInput.value);
    const secondNumber = toNumber(secondNumberInput.value);
    const result = operation(firstNumber, secondNumber);
    resultOutput.textContent = String(result);
  }

  if (addButton) {
    addButton.addEventListener("click", function () {
      updateResult(add);
    });
  }

  if (subtractButton) {
    subtractButton.addEventListener("click", function () {
      updateResult(subtract);
    });
  }

  if (multiplyButton) {
    multiplyButton.addEventListener("click", function () {
      updateResult(multiply);
    });
  }

  if (divideButton) {
    divideButton.addEventListener("click", function () {
      updateResult(divide);
    });
  }
}
