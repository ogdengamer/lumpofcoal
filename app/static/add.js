// Gift forms (add dialog, /gifts/new, the edit page) and small page helpers.
// Everything works without JS (minus photos); this makes it fast: photo uploads
// start immediately and run while you pick a recipient and price.
"use strict";

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-gift-form]").forEach(initGiftForm);

  // The + button: on a phone it's one tap to the camera (it labels a capture input),
  // and backing out of the camera opens the form with no photo. With a mouse
  // (a computer) it opens the form directly; Camera/Gallery are inside.
  const fab = document.getElementById("fab-camera");
  const fabButton = document.querySelector("label.fab");
  const addForm = document.getElementById("add-form");
  if (fab && addForm) {
    const dialog = document.getElementById("add-dialog");
    const openSheet = () => {
      if (dialog.open) return;
      dialog.showModal();
      document.activeElement?.blur(); // no keyboard popping up over the form
    };
    fab.addEventListener("change", () => {
      const file = takeFile(fab);
      if (!file) return;
      openSheet();
      addForm.startUpload(file);
    });
    fab.addEventListener("cancel", openSheet); // backed out of the camera
    fabButton.addEventListener("click", (event) => {
      if (window.matchMedia("(pointer: coarse)").matches) return; // phone: camera
      event.preventDefault();
      openSheet();
    });
  }

  // Filter dropdowns apply as soon as they change.
  document.querySelectorAll("form[data-auto-submit]").forEach((form) => {
    form.addEventListener("change", (event) => {
      if (event.target.type !== "search") form.requestSubmit();
    });
  });

  // Destructive buttons ask first.
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  document.querySelectorAll("button[data-confirm]").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm(button.dataset.confirm)) event.preventDefault();
    });
  });

  // Copy buttons (the sign-in link in Settings > App users).
  document.querySelectorAll("button[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const input = document.querySelector(button.dataset.copy);
      input.select();
      try {
        await navigator.clipboard.writeText(input.value);
      } catch {
        document.execCommand("copy"); // older browsers, or no clipboard permission
      }
      button.textContent = "Copied ✓";
    });
  });

  // Settings rows save as you go: on leaving a field or picking from a list.
  document.querySelectorAll("form[data-autosave]").forEach(initAutosave);

  // Settings: "for the group / each" only applies to group budgets...
  document.querySelectorAll("[data-rule-form]").forEach((form) => {
    const target = form.querySelector("[data-target]");
    const basis = form.querySelector("[data-basis]");
    const sync = () => { basis.hidden = !target.selectedOptions[0]?.hasAttribute("data-group"); };
    target.addEventListener("change", sync);
    sync();
  });
  // ...and month/day only to "same date every year".
  document.querySelectorAll("[data-date-rule]").forEach((rule) => {
    const date = rule.parentElement.querySelector("[data-fixed-date]");
    const sync = () => { date.hidden = rule.value !== "fixed"; };
    rule.addEventListener("change", sync);
    sync();
  });
});

function initAutosave(form) {
  const status = form.querySelector("[data-row-status]");
  let queue = Promise.resolve(); // one save at a time, in order
  let fade;

  function show(text, kind = "") {
    clearTimeout(fade);
    status.textContent = text;
    status.className = "row-status " + kind;
  }

  // A month without a day (or the reverse) isn't an error yet: wait for the other half.
  function waitingFor() {
    for (const pair of form.querySelectorAll("[data-pair]")) {
      if (pair.hidden) continue;
      const fields = [...pair.querySelectorAll("input, select")];
      const empty = fields.filter((f) => !f.value.trim());
      if (empty.length && empty.length < fields.length) {
        return "Add the " + (empty[0].name.endsWith("month") ? "month" : "day");
      }
      if (empty.length && pair.dataset.pair === "required") return "Pick the date";
    }
    return null;
  }

  async function save() {
    const wait = waitingFor();
    if (wait) { show(wait + "…", "waiting"); return; }
    show("Saving…");
    try {
      const resp = await fetch(form.action, {
        method: "POST", body: new FormData(form), credentials: "same-origin",
        headers: { "X-Autosave": "1" },
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { show(data.error || "Couldn't save", "error"); return; }
      show("Saved ✓", "ok");
      fade = setTimeout(() => show(""), 2500);
      const active = form.querySelector('input[type=checkbox][name="active"]');
      if (active) form.classList.toggle("inactive", !active.checked);
    } catch {
      show("Couldn't save (offline?)", "error");
    }
  }

  form.addEventListener("change", (event) => {
    if (event.target.name) queue = queue.then(save);
  });
  form.addEventListener("submit", (event) => {
    if (event.submitter?.hasAttribute("formaction")) return; // Delete: a real post
    event.preventDefault(); // Enter in a field: save in place
    queue = queue.then(save);
  });
}

function takeFile(input) {
  const file = input.files && input.files[0];
  input.value = ""; // allow picking the same file again
  return file;
}

function initGiftForm(form) {
  const $ = (sel) => form.querySelector(sel);
  const dialog = form.closest("dialog");

  const preview = $("[data-preview]");
  const status = $("[data-photo-status]");
  const photoId = $("[data-photo-id]");
  const save = $("[data-save]");
  const saveLabel = save.textContent;
  const price = $("[data-price]");
  const occasion = $("[data-occasion]");
  const source = $("[data-source]");
  const hint = $("[data-group-hint]");
  const groupName = $("[data-group-name]");
  const hints = JSON.parse(form.dataset.hints || "{}");
  const chips = () => [...form.querySelectorAll('input[name="recipients"]')];

  // --- photo -------------------------------------------------------------
  let upload = null;          // in-flight upload promise
  let submitWhenDone = false; // Save tapped while uploading

  function setSaving(busy, label) {
    save.disabled = busy;
    save.textContent = label || saveLabel;
  }

  form.startUpload = (file) => {
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
    photoId.value = "";
    status.textContent = "Uploading photo…";
    const body = new FormData();
    body.append("file", file);
    const attempt = fetch("/photos", { method: "POST", body, credentials: "same-origin" })
      .then(async (resp) => {
        if (!resp.ok) throw new Error((await resp.text()) || resp.statusText);
        return resp.json();
      })
      .then((photo) => {
        if (upload !== attempt) return; // superseded by a retake
        photoId.value = photo.id;
        status.textContent = "";
        upload = null; // before resubmitting, or the submit handler would wait again
        if (submitWhenDone) form.requestSubmit();
      })
      .catch((err) => {
        if (upload !== attempt) return;
        status.textContent = "Photo didn't upload (" + err.message + "). Retake, or save without it.";
        submitWhenDone = false;
        setSaving(false);
      })
      .finally(() => { if (upload === attempt) upload = null; });
    upload = attempt;
  };

  form.querySelectorAll(".photo-input").forEach((input) => {
    input.addEventListener("change", () => {
      const file = takeFile(input);
      if (file) form.startUpload(file);
    });
  });

  form.addEventListener("submit", (event) => {
    if (upload) {
      event.preventDefault();
      submitWhenDone = true;
      setSaving(true, "Saving after upload…");
    } else {
      setSaving(true, "Saving…");
    }
  });

  // Back/forward cache can restore a page mid-"Saving…"; reset the button.
  window.addEventListener("pageshow", () => setSaving(false));

  form.querySelector("[data-close]")?.addEventListener("click", () => dialog?.close());

  // --- recipients ----------------------------------------------------------
  // Pick one or several. The first pick jumps to the cost (keyboard up), since
  // most gifts are for one person; tapping another chip adds them too.
  form.addEventListener("change", (event) => {
    const chip = event.target;
    if (chip.name !== "recipients") return;
    if (chip.checked && chips().filter((c) => c.checked).length === 1) price.focus();
    refresh();
  });

  // Less-used recipients fold behind a "…" chip.
  const chipRow = form.querySelector("[data-fold-rest]");
  const moreChips = $("[data-more-chips]");
  function unfold() {
    chipRow?.removeAttribute("data-folded");
    moreChips.hidden = true;
  }
  if (chipRow) {
    chipRow.setAttribute("data-folded", "");
    moreChips.hidden = false;
    moreChips.addEventListener("click", unfold);
  }

  // "+ New": type a name, get a picked chip for them. The person is created with the gift.
  const newChip = $("[data-new-chip]");
  const newCheck = newChip.querySelector("input");
  const newButton = $("[data-new-person]");
  const newBox = $("[data-new-person-box]");
  const newName = $("[data-new-name]");

  function addNewPerson() {
    const name = newName.value.trim().replace(/\s+/g, " ");
    if (!name) { newName.focus(); return; }
    newName.value = name;
    newChip.querySelector("[data-new-label]").textContent = name;
    newCheck.dataset.label = name;
    newChip.hidden = false;
    newBox.hidden = true;
    newButton.hidden = true;
    newCheck.checked = true;
    newCheck.dispatchEvent(new Event("change", { bubbles: true })); // same as tapping a chip
  }

  newButton.addEventListener("click", () => {
    newBox.hidden = false;
    newName.focus();
  });
  $("[data-new-add]").addEventListener("click", addNewPerson);
  newName.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addNewPerson(); } // not "save the gift"
  });
  // Unpicking the new chip forgets them, and offers "+ New" again.
  newCheck.addEventListener("change", () => {
    if (newCheck.checked) return;
    newChip.hidden = true;
    newButton.hidden = false;
    newName.value = "";
  });

  function selectGroup(groupId) {
    chips().forEach((c) => {
      c.checked = c.dataset.kind === "group" && c.dataset.id === String(groupId);
      if (c.checked && c.closest(".rest-chip")) unfold();
    });
    if (!newChip.hidden) newCheck.dispatchEvent(new Event("change")); // unpicked: tidy away
    refresh();
  }

  function refresh() {
    const checked = chips().filter((c) => c.checked);
    const people = checked.filter((c) => c.dataset.kind === "person");

    // Several people chosen: offer to name them as a group.
    groupName.hidden = !(checked.length > 1);

    // One person chosen who's in a group budget for this occasion type.
    const typeId = Number(occasion.selectedOptions[0]?.dataset.type);
    const match = checked.length === 1 && people.length === 1 &&
      (hints[people[0].dataset.id] || []).find((h) => h.occasion_type_id === typeId);
    hint.hidden = !match;
    if (match) {
      const amount = match.basis === "group_total" ? "$" + match.amount + " for the group"
                                                   : "$" + match.amount + " each";
      hint.querySelector("[data-hint-text]").textContent =
        people[0].dataset.label + " is in the " + match.group + " budget (" + amount + ").";
      hint.querySelector("[data-use-group]").onclick = () => selectGroup(match.group_id);
    }
  }

  occasion.addEventListener("change", refresh);
  refresh();

  // --- source --------------------------------------------------------------
  // The 4 most recent sources as chips. Typing filters them; until you type,
  // the pre-filled value doesn't, so the other recent sources stay one tap away.
  const sourceRow = $("[data-source-chips]");
  const sourceChips = [...sourceRow.querySelectorAll("[data-value]")];
  let typed = false;

  function showSources() {
    const text = typed ? source.value.trim().toLowerCase() : "";
    let shown = 0;
    sourceChips.forEach((c) => {
      const show = shown < 4 && c.dataset.value.toLowerCase().includes(text);
      c.hidden = !show;
      if (show) shown++;
      c.classList.toggle("on", c.dataset.value === source.value.trim());
    });
    sourceRow.hidden = shown === 0;
  }

  source.addEventListener("input", () => { typed = true; showSources(); });
  sourceChips.forEach((c) => c.addEventListener("click", () => {
    source.value = c.dataset.value;
    typed = false;
    showSources();
  }));
  showSources();
}
