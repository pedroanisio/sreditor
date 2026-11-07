// Small enhancements for the signed-in pages; every page works without them.
// - A textarea with data-limit keeps its counter live and marks it when over the limit; the
//   server renders the same counter, so it is right before this runs and without JavaScript.
// - A button with data-copy copies the named field and says so for a moment (rule
//   ui.microinteractions); it stays hidden when the browser cannot copy, and selects the field
//   when the browser refuses.
// - A field with data-select-on-focus selects its whole value, so it can be copied by hand.
// - A button with data-dialog opens that dialog as a modal box; a dialog with data-open opens on
//   load, as after a form error, and a click on the dimmed page around it closes it.
// - A toast leaves after a while, but not while the pointer or focus is on it; without
//   JavaScript it stays.
// - When a form had expired, the server keeps the page as it is and sends a fresh token
//   (event sreditor-form-renewed): every form gets it and the form shows why it did not go.
// - A file field with data-max-bytes refuses a larger file as soon as it is chosen, with the
//   server's own message, instead of sending it for nothing; the server still checks.
// - When the sidebar's links become a row that scrolls sideways (narrow screens), the row fades
//   at the edge where more links wait, and the current link scrolls into view.
(function () {
  "use strict";

  var COPIED_FOR_MS = 2000;
  var TOAST_FOR_MS = 6000;
  var ANNOUNCE_AFTER_MS = 100;

  function bindCounter(field) {
    var output = document.querySelector("[data-counter-for='" + field.id + "']");
    var limit = parseInt(field.getAttribute("data-limit"), 10);
    var from = parseInt(field.getAttribute("data-counter-from"), 10) || 0;
    if (!output || !limit) {
      return;
    }
    function update() {
      var used = field.value.length;
      output.textContent = used + "/" + limit;
      output.classList.toggle("over", used > limit);
      output.classList.toggle("is-quiet", used < from);
    }
    field.addEventListener("input", update);
    update();
  }

  function bindCopy(button) {
    if (!navigator.clipboard) {
      return;
    }
    var source = document.getElementById(button.getAttribute("data-copy"));
    var label = button.textContent;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(source.value).then(
        function () {
          button.textContent = button.getAttribute("data-copied");
          window.setTimeout(function () {
            button.textContent = label;
          }, COPIED_FOR_MS);
        },
        function () {
          // The browser refused; select the value so it can be copied by hand.
          source.focus();
          source.select();
        }
      );
    });
  }

  function bindFileLimit(input) {
    var limit = parseInt(input.getAttribute("data-max-bytes"), 10);
    var field = input.closest(".field");
    var message = document.createElement("p");
    message.className = "error";
    message.id = input.id + "-limit";
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      var isTooLarge = Boolean(file && file.size > limit);
      field.classList.toggle("invalid", isTooLarge);
      if (isTooLarge) {
        message.textContent = input.getAttribute("data-too-large");
        field.appendChild(message);
        input.setAttribute("aria-invalid", "true");
        input.setAttribute("aria-describedby", message.id);
        input.value = "";
      } else if (message.parentNode) {
        message.remove();
        input.removeAttribute("aria-invalid");
      }
    });
  }

  function bindSelect(field) {
    field.addEventListener("focus", function () {
      field.select();
    });
  }

  function bindDialogButton(button) {
    var dialog = document.getElementById(button.getAttribute("data-dialog"));
    if (!dialog || !dialog.showModal) {
      return;
    }
    button.addEventListener("click", function () {
      dialog.showModal();
    });
  }

  function bindDialog(dialog) {
    if (!dialog.showModal) {
      return;
    }
    dialog.addEventListener("click", function (event) {
      // The dialog element itself is only hit on its backdrop; its content sits in children.
      if (event.target === dialog) {
        dialog.close();
      }
    });
    if (dialog.hasAttribute("data-open")) {
      dialog.showModal();
    }
  }

  function bindToast(toast) {
    var timer = null;
    // A live region announces changes, not what it held on load, so the text arrives just after.
    var text = toast.querySelector("span");
    var message = text.textContent;
    text.textContent = "";
    window.setTimeout(function () {
      text.textContent = message;
    }, ANNOUNCE_AFTER_MS);
    function leave() {
      toast.classList.add("is-leaving");
      // With reduced motion the animation takes no time, so animationend may not come.
      toast.addEventListener("animationend", function () {
        toast.remove();
      });
      window.setTimeout(function () {
        toast.remove();
      }, TOAST_FOR_MS);
    }
    function wait() {
      timer = window.setTimeout(leave, TOAST_FOR_MS);
    }
    function hold() {
      window.clearTimeout(timer);
    }
    toast.addEventListener("mouseenter", hold);
    toast.addEventListener("mouseleave", wait);
    wait();
  }

  function bindScrollRow(row) {
    function update() {
      var hidden = row.scrollWidth - row.clientWidth;
      row.classList.toggle("is-clipped-start", row.scrollLeft > 1);
      row.classList.toggle("is-clipped-end", hidden - row.scrollLeft > 1);
    }
    var current = row.querySelector("[aria-current='page']");
    if (current && row.scrollWidth > row.clientWidth) {
      var offset = current.getBoundingClientRect().left - row.getBoundingClientRect().left;
      row.scrollLeft += offset - (row.clientWidth - current.offsetWidth) / 2;
    }
    row.addEventListener("scroll", update, { passive: true });
    // Observing the row itself, not the window, so listeners go away with the page htmx swaps.
    if (window.ResizeObserver) {
      new ResizeObserver(update).observe(row);
    }
    update();
  }

  // The field name is CSRF_FIELD in sreditor/web/forms.py.
  document.addEventListener("sreditor-form-renewed", function (event) {
    document.querySelectorAll("input[name='csrf_token']").forEach(function (field) {
      field.value = event.detail.token;
    });
    var form = event.target.closest ? event.target.closest("form") : null;
    if (!form) {
      return;
    }
    var notice = form.querySelector("[data-renewed]") || document.createElement("div");
    notice.className = "alert error";
    notice.setAttribute("role", "alert");
    notice.setAttribute("data-renewed", "");
    notice.textContent = event.detail.message;
    form.prepend(notice);
  });

  // htmx also fires htmx:load for the first page, so bind each element only once.
  function once(bind) {
    return function (element) {
      if (element.hasAttribute("data-enhanced")) {
        return;
      }
      element.setAttribute("data-enhanced", "");
      bind(element);
    };
  }

  function enhance(root) {
    if (!root.querySelectorAll) {
      return;
    }
    root.querySelectorAll("textarea[data-limit]").forEach(once(bindCounter));
    root.querySelectorAll("[data-copy]").forEach(once(bindCopy));
    root.querySelectorAll("[data-select-on-focus]").forEach(once(bindSelect));
    root.querySelectorAll("input[type=file][data-max-bytes]").forEach(once(bindFileLimit));
    root.querySelectorAll("button[data-dialog]").forEach(once(bindDialogButton));
    root.querySelectorAll("dialog.dialog").forEach(once(bindDialog));
    root.querySelectorAll(".toast:not(.is-static)").forEach(once(bindToast));
    root.querySelectorAll(".app-sidebar .sidenav, .tabs ul").forEach(once(bindScrollRow));
  }

  document.addEventListener("DOMContentLoaded", function () {
    enhance(document);
  });
  // Pages that htmx swaps in (hx-boost) need the same treatment.
  document.addEventListener("htmx:load", function (event) {
    enhance(event.target);
  });
})();
