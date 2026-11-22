"use strict";
function applyTheme(preference) {
  if (preference === "light" || preference === "dark") {
    document.documentElement.dataset.theme = preference;
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}
applyTheme(document.currentScript.dataset.preference);
