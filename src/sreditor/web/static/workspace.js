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
// - A form with data-autosave (a text part) saves itself a moment after typing stops, sending
//   the revision it last saved; the Save button stays for when this does not run. When someone
//   else changed the text meanwhile (409), it stops saving and says so, so nothing is lost.
// - On the episode board, picking a stage on a card moves it at once (its Move button then
//   hides), and a card dragged onto another column moves there through the same form.
// - Ctrl+K (Cmd+K on a Mac), or the sidebar's Search link, opens the quick search dialog instead
//   of the search page; arrow keys move between its results.
// - A menu closes when one of its choices is picked, on a click elsewhere and on Escape.
// - A 3D model viewer loads model-viewer.js (and three.js with it) only on the pages that show
//   one, from this script's own origin, so the CSP stays self-only.
(function () {
  "use strict";

  // Taken now: document.currentScript is only set while this file first runs.
  var VIEWER_MODULE = document.currentScript
    ? new URL("model-viewer.js", document.currentScript.src).href
    : "";

  var COPIED_FOR_MS = 2000;
  var TOAST_FOR_MS = 6000;
  var ANNOUNCE_AFTER_MS = 100;
  var AUTOSAVE_AFTER_MS = 1500;
  var AUTOSAVE_HEADER = "Sreditor-Autosave";
  var REVISION_HEADER = "Sreditor-Revision";
  var HTTP_NO_CONTENT = 204;
  var HTTP_CONFLICT = 409;
  var HTTP_UNPROCESSABLE = 422;

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

  var SEARCH_DIALOG = "search";
  var SEARCH_SHORTCUT = "k";

  // Opens the quick search, asks for its first results and puts the cursor in its field.
  function openSearch() {
    var dialog = document.getElementById(SEARCH_DIALOG);
    if (!dialog || !dialog.showModal) {
      return false;
    }
    if (!dialog.open) {
      dialog.showModal();
    }
    var input = dialog.querySelector("input[name=q]");
    input.select();
    input.dispatchEvent(new Event("sreditor-search-open"));
    return true;
  }

  function bindSearchTrigger(link) {
    if (/Mac|iPhone|iPad/.test(navigator.platform)) {
      link.querySelector("kbd").textContent = "\u2318 K";
    }
    link.addEventListener("click", function (event) {
      if (openSearch()) {
        event.preventDefault();
      }
    });
  }

  // Arrow keys walk from the field through the results; Enter in the field opens the first one.
  // The listener sits on the field's form's parent, which holds the results too (dialog or page).
  function bindSearchBox(form) {
    var area = form.parentElement;
    area.addEventListener("keydown", function (event) {
      var stops = [form.querySelector("input[name=q]")].concat(
        Array.prototype.slice.call(area.querySelectorAll(".search-result"))
      );
      var position = stops.indexOf(document.activeElement);
      if (position < 0) {
        return;
      }
      if (event.key === "Enter" && position === 0 && stops.length > 1) {
        event.preventDefault();
        stops[1].click();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        var step = event.key === "ArrowDown" ? 1 : -1;
        stops[(position + step + stops.length) % stops.length].focus();
      }
    });
  }

  // A menu is a <details>: it stays open until closed, so a choice that keeps the page (a
  // download), a click elsewhere or Escape closes it, and it never covers the page behind it.
  function closeMenus(except) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (menu !== except) {
        menu.removeAttribute("open");
      }
    });
  }

  document.addEventListener("click", function (event) {
    var menu = event.target.closest ? event.target.closest("details.menu") : null;
    if (menu && event.target.closest(".menu-list a, .menu-list button")) {
      menu.removeAttribute("open");
      return;
    }
    closeMenus(menu);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var open = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest("details.menu[open]")
      : null;
    closeMenus(null);
    if (open) {
      open.querySelector("summary").focus();
    }
  });

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === SEARCH_SHORTCUT) {
      if (openSearch()) {
        event.preventDefault();
      }
    }
  });

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

  function bindAutosave(form) {
    var status = form.querySelector(".autosave-status");
    var revision = form.querySelector("input[name='revision']");
    var timer = null;
    var isSaving = false;
    var isPending = false;
    var isPaused = false;
    if (!status || !revision) {
      return;
    }
    function say(key, isError) {
      var text = status.getAttribute("data-" + key);
      if (key === "saved") {
        var time = new Date().toLocaleTimeString(document.documentElement.lang, {
          hour: "2-digit",
          minute: "2-digit",
        });
        text = text.replace("%(time)s", time);
      }
      status.textContent = text;
      status.classList.toggle("is-error", Boolean(isError));
    }
    function finished(response) {
      if (response.status === HTTP_NO_CONTENT) {
        revision.value = response.headers.get(REVISION_HEADER);
        say("saved");
        // Parts of the page that show the saved text, such as the screenplay preview, reload.
        document.body.dispatchEvent(new CustomEvent("sreditor-autosaved"));
      } else if (response.status === HTTP_CONFLICT) {
        isPaused = true;
        say("changed", true);
      } else if (response.status === HTTP_UNPROCESSABLE) {
        say("invalid", true);
      } else {
        say("failed", true);
      }
    }
    function save() {
      if (isPaused) {
        return;
      }
      if (isSaving) {
        isPending = true;
        return;
      }
      isSaving = true;
      say("saving");
      var headers = {};
      headers[AUTOSAVE_HEADER] = "1";
      fetch(form.action, { method: "POST", body: new FormData(form), headers: headers, credentials: "same-origin" })
        .then(finished, function () {
          say("failed", true);
        })
        .then(function () {
          isSaving = false;
          if (isPending) {
            isPending = false;
            save();
          }
        });
    }
    form.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(save, AUTOSAVE_AFTER_MS);
    });
    form.addEventListener("submit", function () {
      window.clearTimeout(timer);
    });
  }

  function bindBoardMove(form) {
    var select = form.querySelector("select");
    form.querySelector("button").hidden = true;
    select.addEventListener("change", function () {
      form.requestSubmit();
    });
  }

  function bindBoardCard(card) {
    card.addEventListener("dragstart", function (event) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.querySelector("select").id);
      card.classList.add("is-dragging");
    });
    card.addEventListener("dragend", function () {
      card.classList.remove("is-dragging");
    });
  }

  function bindBoardColumn(column) {
    var stage = column.getAttribute("data-stage");
    column.addEventListener("dragover", function (event) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      column.classList.add("is-drop-target");
    });
    column.addEventListener("dragleave", function (event) {
      if (!column.contains(event.relatedTarget)) {
        column.classList.remove("is-drop-target");
      }
    });
    column.addEventListener("drop", function (event) {
      event.preventDefault();
      column.classList.remove("is-drop-target");
      var select = document.getElementById(event.dataTransfer.getData("text/plain"));
      if (select && select.value !== stage) {
        select.value = stage;
        select.form.requestSubmit();
      }
    });
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
    root.querySelectorAll("[data-search-open]").forEach(once(bindSearchTrigger));
    root.querySelectorAll("form.search-box").forEach(once(bindSearchBox));
    root.querySelectorAll(".app-sidebar .sidenav, .tabs ul").forEach(once(bindScrollRow));
    root.querySelectorAll("form[data-autosave]").forEach(once(bindAutosave));
    root.querySelectorAll("form.board-move").forEach(once(bindBoardMove));
    root.querySelectorAll("[data-board-card]").forEach(once(bindBoardCard));
    root.querySelectorAll(".board-column[data-stage]").forEach(once(bindBoardColumn));
    loadViewers(root);
  }

  function loadViewers(root) {
    var viewers = root.querySelectorAll("[data-model-viewer]:not([data-enhanced])");
    if (!viewers.length || !VIEWER_MODULE) {
      return;
    }
    import(VIEWER_MODULE).then(
      function (module) {
        viewers.forEach(once(module.mount));
      },
      function () {
        viewers.forEach(function (viewer) {
          viewer.classList.replace("is-loading", "is-error");
          var status = viewer.querySelector("[role=status]");
          status.textContent = status.getAttribute("data-error");
        });
      }
    );
  }

  document.addEventListener("DOMContentLoaded", function () {
    enhance(document);
  });
  // Pages that htmx swaps in (hx-boost) need the same treatment.
  document.addEventListener("htmx:load", function (event) {
    enhance(event.target);
  });
})();
