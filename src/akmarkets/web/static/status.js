/* Live status bar and activity feed.
 *
 * Progressive enhancement, not a framework: the page is complete and
 * readable without this file, and this only fills in what is happening
 * right now. It is excluded entirely from the static export, where there is
 * no server to stream from.
 *
 * Reconnection is handled by EventSource itself, and correctness on
 * reconnect comes from the server contract rather than from state kept
 * here: the stream is resumable by event id, so a dropped connection costs
 * at most the events between the last one seen and the reconnect.
 */
(function () {
  "use strict";

  var bar = document.getElementById("statusbar");
  var message = document.getElementById("status-message");
  var list = document.getElementById("activity-list");
  if (!bar || !message) return;

  var MAX_ROWS = 40;
  var seeded = false;

  function time(iso) {
    try {
      return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (error) {
      return "";
    }
  }

  function addRow(event) {
    if (!list) return;
    if (!seeded) {
      list.innerHTML = "";
      seeded = true;
    }
    var row = document.createElement("li");
    row.className = "level-" + (event.level || "info");

    var stamp = document.createElement("time");
    stamp.textContent = time(event.at);
    row.appendChild(stamp);

    var text = document.createElement("span");
    text.textContent =
      event.message + (event.percent !== null && event.percent !== undefined ? " (" + event.percent + "%)" : "");
    row.appendChild(text);

    list.insertBefore(row, list.firstChild);
    while (list.childElementCount > MAX_ROWS) {
      list.removeChild(list.lastElementChild);
    }
  }

  function paintStatus(state) {
    bar.setAttribute("data-busy", state.busy ? "true" : "false");
    var text = state.message || "idle";
    if (state.percent !== null && state.percent !== undefined) {
      text += " · " + state.percent + "%";
    }
    message.textContent = text;
  }

  fetch("/api/status")
    .then(function (response) { return response.json(); })
    .then(paintStatus)
    .catch(function () { /* the status bar is not worth an error banner */ });

  var stream = new EventSource("/api/activity/stream");
  stream.onmessage = function (frame) {
    var event;
    try {
      event = JSON.parse(frame.data);
    } catch (error) {
      return;
    }
    paintStatus({
      busy: true,
      message: event.message,
      percent: event.percent
    });
    addRow(event);
  };
  stream.onerror = function () {
    bar.setAttribute("data-busy", "false");
  };
})();
