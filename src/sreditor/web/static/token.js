// Email links carry their one-time token in the URL fragment (#token=...), which browsers never
// send to the server, so it stays out of access logs, proxies and Referer headers (rule
// web.email.links). This copies it into the page's token field, removes it from the address
// bar, and submits the form when the form asks for that. With no token to copy, it shows the
// field so the person can paste the link instead.
(function () {
  "use strict";

  var TOKEN_IN_FRAGMENT = /(?:^#|&)token=([^&]+)/;

  function fill(root) {
    var form = root.querySelector && root.querySelector("form[data-token-form]");
    if (!form) {
      return;
    }
    var field = form.querySelector("input[name='token']");
    var match = TOKEN_IN_FRAGMENT.exec(window.location.hash);
    if (match) {
      field.value = decodeURIComponent(match[1]);
      history.replaceState(null, "", window.location.pathname + window.location.search);
      if (form.hasAttribute("data-auto-submit")) {
        form.requestSubmit();
      }
    } else if (!field.value) {
      form.querySelector("[data-token-paste]").hidden = false;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    fill(document);
  });
  // Pages that htmx swaps in (hx-boost) need the same treatment.
  document.addEventListener("htmx:load", function (event) {
    fill(event.target);
  });
})();
