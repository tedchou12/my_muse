"use strict";

/* ------------------------------------------------------------------ *
 *  Muse UI — dependency-free vanilla JS
 * ------------------------------------------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  session: null,
  tab: "chat",
  chatId: "default",
  chatAgentId: null,
  chatGroup: null,
  groups: [],
  runs: {},
  chatErrors: {},
  agents: [],
  tasks: [],
  team: null,
  teamTaskId: null,
  approvals: { pending: [], recent: [] },
  vault: null,
  browserTimer: null,
  browserSession: "default",
  vaultTimer: null,
  teamTimer: null,
  groupTimer: null,
  badgeTimer: null,
  chatTimer: null,
  provider: null,
  providerPreset: null,
  pendingAttachments: [],
  uploadsInProgress: 0,
};

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function mdInline(raw) {
  const tokens = [];
  let text = String(raw === null || raw === undefined ? "" : raw);
  text = text.replace(/`([^`\n]+)`/g, (match, code) => {
    tokens.push("<code>" + esc(code) + "</code>");
    return "\u0001" + (tokens.length - 1) + "\u0001";
  });
  text = text.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (match, label, url) => {
      tokens.push(
        '<a href="' +
          esc(url) +
          '" target="_blank" rel="noopener noreferrer">' +
          esc(label) +
          "</a>"
      );
      return "\u0001" + (tokens.length - 1) + "\u0001";
    }
  );
  text = esc(text);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  text = text.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  text = text.replace(/_([^_\n]+)_/g, "<em>$1</em>");
  text = text.replace(/\u0001(\d+)\u0001/g, (match, index) => {
    return tokens[Number(index)] || "";
  });
  return text;
}

function mdCells(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isMdTableSeparator(line) {
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
}

function renderMarkdown(source) {
  const lines = String(source === null || source === undefined ? "" : source)
    .replace(/\r\n?/g, "\n")
    .split("\n");
  const out = [];
  let paragraph = [];
  let list = null;
  let code = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push("<p>" + paragraph.map(mdInline).join("<br>") + "</p>");
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const tag = list.ordered ? "ol" : "ul";
    out.push(
      "<" +
        tag +
        ">" +
        list.items.map((item) => "<li>" + mdInline(item) + "</li>").join("") +
        "</" +
        tag +
        ">"
    );
    list = null;
  };
  const flushBlocks = () => {
    flushParagraph();
    flushList();
  };

  let index = 0;
  while (index < lines.length) {
    const raw = lines[index];
    const line = raw.trim();

    if (code !== null) {
      if (line.startsWith("```")) {
        out.push('<pre class="md-code"><code>' + esc(code.join("\n")) + "</code></pre>");
        code = null;
      } else {
        code.push(raw);
      }
      index += 1;
      continue;
    }

    if (line.startsWith("```")) {
      flushBlocks();
      code = [];
      index += 1;
      continue;
    }

    if (!line) {
      flushBlocks();
      index += 1;
      continue;
    }

    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      isMdTableSeparator(lines[index + 1])
    ) {
      const header = mdCells(raw);
      const separator = mdCells(lines[index + 1]);
      if (header.length >= 2 && separator.length === header.length) {
        flushBlocks();
        const align = separator.map((cell) => {
          const left = cell.startsWith(":");
          const right = cell.endsWith(":");
          return left && right ? "center" : right ? "right" : left ? "left" : "";
        });
        index += 2;
        const rows = [];
        while (
          index < lines.length &&
          lines[index].trim() &&
          lines[index].includes("|")
        ) {
          rows.push(mdCells(lines[index]));
          index += 1;
        }
        const cell = (tag, value, column) =>
          "<" +
          tag +
          (align[column] ? ' class="md-align-' + align[column] + '"' : "") +
          ">" +
          mdInline(value) +
          "</" +
          tag +
          ">";
        out.push(
          '<div class="md-table-wrap"><table><thead><tr>' +
            header.map((value, column) => cell("th", value, column)).join("") +
            "</tr></thead><tbody>" +
            rows
              .map(
                (row) =>
                  "<tr>" +
                  header
                    .map((value, column) => cell("td", row[column] || "", column))
                    .join("") +
                  "</tr>"
              )
              .join("") +
            "</tbody></table></div>"
        );
        continue;
      }
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushBlocks();
      const level = heading[1].length;
      out.push(
        "<h" + level + ">" + mdInline(heading[2]) + "</h" + level + ">"
      );
      index += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushBlocks();
      out.push("<hr>");
      index += 1;
      continue;
    }

    if (line.startsWith(">")) {
      flushBlocks();
      const quote = [];
      while (index < lines.length && lines[index].trim().startsWith(">")) {
        quote.push(lines[index].trim().replace(/^>\s?/, ""));
        index += 1;
      }
      out.push(
        "<blockquote>" + quote.map(mdInline).join("<br>") + "</blockquote>"
      );
      continue;
    }

    const listItem = raw.match(/^\s*(?:[-*+]|\d+\.)\s+(.*)$/);
    if (listItem) {
      flushParagraph();
      const ordered = /^\s*\d+\./.test(raw);
      if (!list || list.ordered !== ordered) {
        flushList();
        list = { ordered: ordered, items: [] };
      }
      list.items.push(listItem[1]);
      index += 1;
      continue;
    }

    if (list) flushList();
    paragraph.push(line);
    index += 1;
  }

  if (code !== null) {
    out.push('<pre class="md-code"><code>' + esc(code.join("\n")) + "</code></pre>");
  }
  flushBlocks();
  return out.join("");
}

function fmtTime(ts) {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function fmtWhen(ts) {
  if (!ts) return "—";
  const diff = ts * 1000 - Date.now();
  if (diff <= 0) return "due now";
  const minutes = Math.round(diff / 60000);
  if (minutes < 60) return "in " + minutes + " min";
  const hours = Math.round(minutes / 60);
  if (hours < 48) return "in " + hours + " h";
  return fmtTime(ts);
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  el.classList.toggle("bad", Boolean(bad));
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, options = {}) {
  const opts = Object.assign({ credentials: "same-origin" }, options);
  opts.headers = Object.assign({}, options.headers || {});
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    opts.headers["X-Muse-Request"] = "1";
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (error) {
    const wrapped = new Error(
      "Muse is unreachable. The container may be restarting or the request was interrupted. Retry in a moment."
    );
    wrapped.status = 0;
    throw wrapped;
  }
  const type = res.headers.get("content-type") || "";
  let data = "";
  if (type.includes("application/json")) {
    data = await res.json();
  } else {
    data = await res.text();
  }
  if (!res.ok) {
    let message = "Request failed (" + res.status + ")";
    if (data && typeof data === "object" && data.detail) {
      message = Array.isArray(data.detail)
        ? data.detail.map((item) => item.msg || JSON.stringify(item)).join("; ")
        : String(data.detail);
    } else if (typeof data === "string" && data.trim()) {
      message = data.slice(0, 300);
    }
    if (res.status === 401 && !path.includes("/api/login")) {
      stopBrowserPolling();
      stopVaultPolling();
      if (state.badgeTimer) {
        clearInterval(state.badgeTimer);
        state.badgeTimer = null;
      }
      state.session = null;
      showLogin();
    }
    const error = new Error(message);
    error.status = res.status;
    throw error;
  }
  return data;
}

/* ---------------------------------------------------------------- auth */
function showLogin() {
  stopChatPolling();
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-password").value = "";
  setTimeout(() => $("#login-password").focus(), 30);
}

function updateSentinel() {
  if (!state.session) return;
  const bits = [state.session.write_enabled ? "write on" : "read-only"];
  if (state.session.auto_approve_writes) bits.push("auto-approve");
  if (state.session.shell_enabled) bits.push("shell");
  $("#sentinel-state").textContent = "Sentinel · " + bits.join(" · ");
  $("#brand-name").textContent = state.session.agent_name || "Muse";
}

function enterApp(session) {
  state.session = session;
  $("#login").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  updateSentinel();
  if (!$("#version").textContent) $("#version").textContent = "v" + (session.version || "");
  switchTab("chat");
  renderAgentNav();
  refreshBadges();
  if (!state.badgeTimer) {
    state.badgeTimer = setInterval(refreshBadges, 20000);
  }
}

async function refreshBadges() {
  try {
    const [tasks, approvals, team, agents, groups] = await Promise.all([
      api("/api/tasks"),
      api("/api/approvals?brief=1"),
      api("/api/team/summary"),
      api("/api/team/agents"),
      api("/api/team/groups"),
    ]);
    state.tasks = tasks.tasks || [];
    state.approvals = {
      ...approvals,
      trusted_commands:
        (state.approvals && state.approvals.trusted_commands) || [],
    };
    state.agents = (agents && agents.agents) || [];
    state.groups = (groups && groups.groups) || [];
    renderAgentNav();
    const activeCount = state.tasks.filter((task) => task.active).length;
    $("#task-count").textContent = activeCount ? String(activeCount) : "";
    const teamCount =
      (team.queued || 0) + (team.working || 0) + (team.needs_approval || 0);
    const teamBadge = $("#team-count");
    if (teamBadge) {
      teamBadge.textContent = teamCount ? String(teamCount) : "";
    }
    const pending =
      approvals.pending_count != null
        ? approvals.pending_count + (approvals.pending_credential_count || 0)
        : (approvals.pending || []).length +
          (approvals.pending_credentials || []).length;
    const badge = $("#approval-count");
    badge.textContent = pending ? String(pending) : "";
    badge.classList.toggle("hidden", pending === 0);
    const settingsDot = $("#settings-dot");
    if (settingsDot) {
      settingsDot.textContent = pending ? String(pending) : "";
      settingsDot.classList.toggle("hidden", pending === 0);
    }
  } catch (error) {
    /* badges are best effort */
  }
}

function renderAgentNav() {
  const list = $("#agent-list");
  if (!list) return;
  const chatActive = state.tab === "chat";
  const leadName = state.session ? state.session.agent_name || "Muse" : "Muse";
  const lead =
    '<button class="agent-chat lead' +
    (chatActive && !state.chatAgentId && !state.chatGroup ? " active" : "") +
    '" data-agent-id="" type="button">' +
    '<span class="agent-avatar">◈</span>' +
    '<span class="agent-copy"><strong>' +
    esc(leadName) +
    "</strong><small>Team lead</small></span>" +
    (state.runs.default ? '<span class="pill warn">running</span>' : "") +
    "</button>";
  const activeAgents = (state.agents || []).filter(
    (agent) => agent.status === "active"
  );
  const rows = activeAgents
    .map((agent) => {
      const initial = esc((agent.name || "?").trim().slice(0, 1).toUpperCase());
      const open = Number(agent.open_tasks || 0);
      const running = Boolean(state.runs["agent:" + agent.id + ":dm"]);
      return (
        '<button class="agent-chat' +
        (chatActive && Number(state.chatAgentId) === Number(agent.id)
          ? " active"
          : "") +
        '" data-agent-id="' +
        esc(agent.id) +
        '" type="button">' +
        '<span class="agent-avatar">' +
        initial +
        "</span>" +
        '<span class="agent-copy"><strong>' +
        esc(agent.name) +
        "</strong><small>" +
        esc(agent.role || "Teammate") +
        "</small></span>" +
        (running
          ? '<span class="pill warn">running</span>'
          : open
          ? '<span class="pill warn">' + open + "</span>"
          : "") +
        "</button>"
      );
    })
    .join("");
  const groupRows = (state.groups || [])
    .map((group) => {
      const active = Boolean(
        state.chatGroup &&
          Number(state.chatGroup.agent_a_id) === Number(group.agent_a_id) &&
          Number(state.chatGroup.agent_b_id) === Number(group.agent_b_id)
      );
      return (
        '<button class="agent-chat group' +
        (chatActive && active ? " active" : "") +
        '" data-group-a="' +
        esc(group.agent_a_id) +
        '" data-group-b="' +
        esc(group.agent_b_id) +
        '" type="button">' +
        '<span class="agent-avatar">⇄</span>' +
        '<span class="agent-copy"><strong>' +
        esc(group.title || "Agent group") +
        "</strong><small>" +
        esc(group.last_message || "You and both agents") +
        "</small></span>" +
        "</button>"
      );
    })
    .join("");
  const groupBlock = groupRows
    ? '<div class="agent-empty group-label">Group chats</div>' + groupRows
    : "";
  list.innerHTML =
    lead +
    (rows || '<div class="agent-empty">No bots yet. Use “＋ New bot” to create one.</div>') +
    groupBlock;
}

async function onAgentChat(event) {
  const groupButton = event.target.closest("[data-group-a][data-group-b]");
  if (groupButton) {
    const a = Number(groupButton.dataset.groupA);
    const b = Number(groupButton.dataset.groupB);
    const group = (state.groups || []).find(
      (item) =>
        Number(item.agent_a_id) === a && Number(item.agent_b_id) === b
    ) || {
      agent_a_id: a,
      agent_b_id: b,
      title: "Agent group",
    };
    setGroupTarget(group);
    switchTab("chat");
    return;
  }
  const button = event.target.closest("[data-agent-id]");
  if (!button) return;
  const id = button.dataset.agentId;
  if (!id) {
    setChatTarget(null, false);
    switchTab("chat");
    return;
  }
  const agent = (state.agents || []).find(
    (item) => Number(item.id) === Number(id)
  );
  if (!agent) return;
  setChatTarget(agent, false);
  switchTab("chat");
}


function switchTab(name) {
  if (name === "browser" && state.chatGroup) {
    toast("Group chats do not have a single browser. Open an agent's chat first.", true);
    name = "chat";
  }
  if (name === "browser") prepareBrowserContext();
  state.tab = name;
  $$(".tab").forEach((el) => el.classList.toggle("active", el.id === "tab-" + name));
  $$("[data-tab]").forEach((button) =>
    button.classList.toggle("active", button.dataset.tab === name)
  );
  renderAgentNav();
  updateChatRunUI();
  stopBrowserPolling();
  stopVaultPolling();
  stopTeamPolling();
  stopGroupPolling();
  stopChatPolling();
  if (name === "chat") {
    loadChat();
    if (state.chatGroup) startGroupPolling();
  }
  if (name === "team") {
    loadTeam();
    startTeamPolling();
  }
  if (name === "tasks") loadTasks();
  if (name === "vault") {
    loadVault();
    startVaultPolling();
  }
  if (name === "browser") loadBrowser();
  if (name === "approvals") loadApprovals();
  if (name === "activity") loadActivity();
  if (name === "provider") loadProvider();
  if (name === "settings") loadSettings();
}

function startVaultPolling() {
  stopVaultPolling();
  state.vaultTimer = setInterval(async () => {
    if (state.tab !== "vault") {
      stopVaultPolling();
      return;
    }
    try {
      const status = await api("/api/vault/status");
      if (!status.unlocked) {
        await loadVault();
      } else {
        $("#vault-timer").textContent = status.seconds_until_lock
          ? "locks in " + Math.ceil(status.seconds_until_lock / 60) + " min"
          : "unlocked";
      }
    } catch (error) {
      /* ignore */
    }
  }, 30000);
}

function stopVaultPolling() {
  if (state.vaultTimer) {
    clearInterval(state.vaultTimer);
    state.vaultTimer = null;
  }
}

function startTeamPolling() {
  stopTeamPolling();
  state.teamTimer = setInterval(async () => {
    if (state.tab !== "team") {
      stopTeamPolling();
      return;
    }
    await loadTeam();
  }, 5000);
}

function stopTeamPolling() {
  if (state.teamTimer) {
    clearInterval(state.teamTimer);
    state.teamTimer = null;
  }
}

/* --------------------------------------------------------------- chat */
async function loadChat() {
  if (state.chatGroup) {
    await loadGroupChat();
    return;
  }
  try {
    const [data, creds, approvals] = await Promise.all([
      api(
        "/api/chat/history?chat_id=" +
          encodeURIComponent(state.chatId) +
          "&limit=100"
      ),
      api(
        "/api/credential-requests?chat_id=" +
          encodeURIComponent(state.chatId) +
          "&status=pending"
      ),
      api("/api/approvals"),
    ]);
    renderChat(data.messages || []);
    (creds.requests || []).forEach(appendCredentialCard);
    (approvals.pending || [])
      .filter((item) => item.chat_id === state.chatId)
      .forEach((item) => appendPending([item]));
    const error = state.chatErrors[state.chatId];
    if (error) {
      appendMessage({
        role: "tool_note",
        content: "Error: " + error,
        created_at: Date.now() / 1000,
      });
      delete state.chatErrors[state.chatId];
    }
    updateChatHeader();
    restoreChatRun();
    await syncChatRunState();
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadGroupChat() {
  const group = state.chatGroup;
  if (!group) return;
  try {
    const data = await api(
      "/api/team/groups/" +
        encodeURIComponent(group.agent_a_id) +
        "/" +
        encodeURIComponent(group.agent_b_id)
    );
    state.chatGroup = data.group || group;
    renderChat(data.messages || []);
    updateChatHeader();
    restoreChatRun();
  } catch (error) {
    toast(error.message, true);
  }
}

function startGroupPolling() {
  stopGroupPolling();
  state.groupTimer = setInterval(async () => {
    if (state.tab !== "chat" || !state.chatGroup) {
      stopGroupPolling();
      return;
    }
    await loadGroupChat();
  }, 5000);
}

function stopGroupPolling() {
  if (state.groupTimer) {
    clearInterval(state.groupTimer);
    state.groupTimer = null;
  }
}

function updateChatRunUI() {
  const stop = $("#chat-stop");
  const send = $("#send");
  const runningHere = Boolean(state.runs[state.chatId]);
  if (stop) stop.classList.toggle("hidden", !runningHere);
  if (send) send.disabled = false;
  if (runningHere && stop) {
    stop.disabled = false;
    stop.textContent = "Stop";
  }
}

function appendWorkingIndicator() {
  const box = $("#messages");
  if (!box || box.querySelector(".typing")) return;
  const typing = document.createElement("div");
  typing.className = "msg assistant typing";
  typing.innerHTML = '<div class="bubble">working</div>';
  box.appendChild(typing);
  box.scrollTop = box.scrollHeight;
}

function removeWorkingIndicators() {
  $$("#messages .typing").forEach((element) => element.remove());
}

function restoreChatRun() {
  updateChatRunUI();
  if (state.runs[state.chatId]) {
    appendWorkingIndicator();
  }
}

function stopChatPolling() {
  if (state.chatTimer) {
    clearInterval(state.chatTimer);
    state.chatTimer = null;
  }
}

async function syncChatRunState() {
  if (!state.chatId || state.chatGroup) {
    stopChatPolling();
    return;
  }
  try {
    const status = await api(
      "/api/chat/status?chat_id=" + encodeURIComponent(state.chatId)
    );
    if (status.active) {
      if (!state.runs[state.chatId]) {
        state.runs[state.chatId] = {
          startedAt: Date.now(),
          agentId: state.chatAgentId,
          remote: true,
        };
      } else {
        state.runs[state.chatId].remote = true;
      }
      appendWorkingIndicator();
      updateChatRunUI();
      renderAgentNav();
      startChatPolling();
    } else {
      const run = state.runs[state.chatId];
      if (run && run.remote) delete state.runs[state.chatId];
      stopChatPolling();
      if (!state.runs[state.chatId]) removeWorkingIndicators();
      updateChatRunUI();
      renderAgentNav();
    }
  } catch (error) {
    /* status is best effort */
  }
}

function startChatPolling() {
  if (state.chatTimer) return;
  state.chatTimer = setInterval(async () => {
    if (state.tab !== "chat" || state.chatGroup) {
      stopChatPolling();
      return;
    }
    try {
      const status = await api(
        "/api/chat/status?chat_id=" + encodeURIComponent(state.chatId)
      );
      if (!status.active) {
        stopChatPolling();
        const run = state.runs[state.chatId];
        if (run && run.remote) delete state.runs[state.chatId];
        if (!state.runs[state.chatId]) removeWorkingIndicators();
        updateChatRunUI();
        renderAgentNav();
        await loadChat();
        return;
      }
      if (!state.runs[state.chatId]) {
        state.runs[state.chatId] = {
          startedAt: Date.now(),
          agentId: state.chatAgentId,
          remote: true,
        };
      } else {
        state.runs[state.chatId].remote = true;
      }
      appendWorkingIndicator();
      updateChatRunUI();
      renderAgentNav();
      await loadChat();
    } catch (error) {
      /* keep the timer alive; a temporary network error should not kill it */
    }
  }, 3000);
}

function currentChatAgent() {
  if (!state.chatAgentId) return null;
  const agents = (state.team && state.team.agents) || state.agents || [];
  return (
    agents.find(
      (agent) => Number(agent.id) === Number(state.chatAgentId)
    ) || null
  );
}

function updateChatHeader() {
  const agent = currentChatAgent();
  const group = state.chatGroup;
  const title = $("#chat-title");
  const subtitle = $("#chat-subtitle");
  const back = $("#chat-back");
  const browserButton = $("#chat-browser");
  const input = $("#chat-input");
  if (!title || !subtitle || !back) return;
  if (group) {
    title.textContent = "Group: " + (group.title || "Agents");
    subtitle.textContent = "You and both agents. Everyone sees this thread.";
  } else {
    title.textContent = agent ? "Chat with " + agent.name : "Chat";
    subtitle.textContent = agent
      ? agent.role || "Named teammate. It can assign tasks and message other agents."
      : "Give goals, not just questions. Muse uses tools to finish them.";
  }
  back.classList.toggle("hidden", !agent && !group);
  if (browserButton) {
    browserButton.classList.toggle("hidden", Boolean(group));
    const owner = agent ? agent.name : state.session ? state.session.agent_name : "Muse";
    browserButton.title = "Open " + (owner || "Muse") + "'s isolated browser window";
  }
  if (input) {
    input.placeholder = group
      ? "Message both agents…"
      : agent
      ? "Ask " + agent.name + " to do something…"
      : "Ask Muse to do something… (Enter sends, Shift+Enter adds a line)";
  }
}

function setGroupTarget(group) {
  state.chatGroup = group;
  state.chatAgentId = null;
  state.chatId = "group:" + group.agent_a_id + ":" + group.agent_b_id;
  updateChatHeader();
  renderAgentNav();
  updateChatRunUI();
}

function setChatTarget(agent, load = true) {
  state.chatGroup = null;
  stopGroupPolling();
  if (agent) {
    state.chatAgentId = Number(agent.id);
    state.chatId = "agent:" + agent.id + ":dm";
  } else {
    state.chatAgentId = null;
    state.chatId = "default";
  }
  updateChatHeader();
  renderAgentNav();
  updateChatRunUI();
  if (load) loadChat();
}


function renderChat(messages) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!messages.length) {
    if (state.chatGroup) {
      box.innerHTML =
        '<div class="empty-chat"><strong>Group chat with ' +
        esc(state.chatGroup.title || "both agents") +
        " and you.</strong><br>Messages between the two agents appear here; you can join in.</div>";
      return;
    }
    const agent = currentChatAgent();
    const name = esc(agent ? agent.name : state.session ? state.session.agent_name : "Muse");
    box.innerHTML =
      '<div class="empty-chat"><strong>Give ' +
      name +
      " a goal.</strong><br>Try “Open news.ycombinator.com and summarise the top five stories” " +
      "or “Every Monday at 8, plan my week and save it as a note.”</div>";
    return;
  }
  messages.forEach(appendMessage);
  box.scrollTop = box.scrollHeight;
}

function appendMessage(message) {
  const box = $("#messages");
  if (box.querySelector(".empty-chat")) box.innerHTML = "";
  const role =
    message.role === "user" ? "user" : message.role === "tool_note" ? "note" : "assistant";
  const wrapper = document.createElement("div");
  wrapper.className = "msg " + role;
  if (message.content) {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (role === "note") {
      bubble.textContent = message.content;
    } else {
      bubble.classList.add("md");
      bubble.innerHTML = renderMarkdown(message.content);
    }
    wrapper.appendChild(bubble);
  }
  (message.attachments || []).forEach((attachment) => {
    const url = attachment.url || attachment.image_url;
    if (!url) return;
    if (attachment.kind === "image" || attachment.image_url) {
      const img = document.createElement("img");
      img.className = "attachment";
      img.src = url;
      img.alt = attachment.name || "Attached image";
      wrapper.appendChild(img);
      if (attachment.name) {
        const caption = document.createElement("div");
        caption.className = "attachment-caption";
        caption.textContent = attachment.name;
        wrapper.appendChild(caption);
      }
      return;
    }
    const link = document.createElement("a");
    link.className = "attachment-file";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent =
      "▤ " +
      (attachment.name || "Attached document") +
      (attachment.size ? " · " + formatBytes(attachment.size) : "");
    wrapper.appendChild(link);
  });
  const meta = document.createElement("div");
  meta.className = "meta";
  const who =
    role === "user"
      ? "you"
      : role === "note"
      ? "system note"
      : message.from_name ||
        (currentChatAgent() && currentChatAgent().name) ||
        (state.session ? state.session.agent_name : "Muse");
  meta.textContent = who + " · " + fmtTime(message.created_at);
  wrapper.appendChild(meta);
  box.appendChild(wrapper);
  box.scrollTop = box.scrollHeight;
  return wrapper;
}

function appendSteps(steps) {
  if (!steps || !steps.length) return;
  const box = $("#messages");
  const details = document.createElement("details");
  details.className = "steps";
  const label = steps.length === 1 ? "1 tool step" : steps.length + " tool steps";
  const rows = steps
    .map((step) => {
      const status =
        step.status === "pending_approval" ? "pending" : step.ok ? "ok" : "error";
      const cls =
        status === "pending" ? "step-pending" : status === "ok" ? "step-ok" : "step-error";
      const args = JSON.stringify(step.args || {});
      return (
        "<li><code>" +
        esc(step.tool) +
        '</code> <span class="' +
        cls +
        '">' +
        esc(status) +
        "</span>" +
        (step.error ? " — " + esc(step.error) : "") +
        "<br><span>" +
        esc(args.slice(0, 260)) +
        "</span></li>"
      );
    })
    .join("");
  details.innerHTML = "<summary>" + esc(label) + "</summary><ul>" + rows + "</ul>";
  box.appendChild(details);
  box.scrollTop = box.scrollHeight;
}

function appendPending(pending) {
  if (!pending || !pending.length) return;
  const box = $("#messages");
  if (box.querySelector(".empty-chat")) box.innerHTML = "";
  pending.forEach((item) => {
    const approvalId = item.approval_id || item.id;
    if (!approvalId) return;
    if (box.querySelector('[data-approval-card="' + approvalId + '"]')) return;
    const host = item.host || "";
    const approveHost = host
      ? '<button class="ghost" data-approval-action="trust-host" data-id="' +
        esc(approvalId) +
        '" data-host="' +
        esc(host) +
        '">Approve on ' +
        esc(host) +
        "</button>"
      : "";
    const div = document.createElement("div");
    div.className = "msg note approval-card";
    div.dataset.approvalCard = String(approvalId);
    div.innerHTML =
      '<div class="bubble">' +
      "<strong>Approval #" +
      esc(approvalId) +
      " needed</strong><br>" +
      esc((item.summary || item.tool) + (host ? " on " + host : "")) +
      '<div class="approval-actions">' +
      '<button class="primary" type="button" data-approval-action="approve" data-id="' +
      esc(approvalId) +
      '">Approve once</button>' +
      approveHost +
      '<button class="danger" type="button" data-approval-action="reject" data-id="' +
      esc(approvalId) +
      '">Reject</button>' +
      '<button class="link" type="button" data-goto-approvals="1">Details</button>' +
      "</div></div>";
    box.appendChild(div);
  });
  box.scrollTop = box.scrollHeight;
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / (1024 * 1024)).toFixed(1) + " MB";
}

function renderPendingAttachments() {
  const box = $("#chat-attachments");
  if (!box) return;
  box.innerHTML = "";
  if (!state.pendingAttachments.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  state.pendingAttachments.forEach((attachment, index) => {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    const preview =
      attachment.kind === "image"
        ? '<img src="' + esc(attachment.url) + '" alt="">'
        : '<span class="attachment-file-icon">▤</span>';
    chip.innerHTML =
      preview +
      '<span class="attachment-label">' +
      esc(attachment.name) +
      ' <span class="attachment-size">· ' +
      esc(formatBytes(attachment.size)) +
      "</span></span>" +
      '<button type="button" data-remove-attachment="' +
      index +
      '" aria-label="Remove attachment">×</button>';
    box.appendChild(chip);
  });
}

function updateAttachmentBusy() {
  const attach = $("#chat-attach");
  const send = $("#send");
  if (attach) attach.disabled = state.uploadsInProgress > 0;
  if (send) send.disabled = state.uploadsInProgress > 0;
}

function clearPendingAttachments() {
  state.pendingAttachments = [];
  renderPendingAttachments();
  updateAttachmentBusy();
}

function onAttachmentClick(event) {
  const button = event.target.closest("[data-remove-attachment]");
  if (!button) return;
  const index = Number(button.dataset.removeAttachment);
  if (!Number.isInteger(index)) return;
  state.pendingAttachments.splice(index, 1);
  renderPendingAttachments();
}

async function uploadChatFile(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  const res = await fetch("/api/uploads", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-Muse-Request": "1" },
    body: form,
  });
  const type = res.headers.get("content-type") || "";
  const data = type.includes("application/json")
    ? await res.json()
    : await res.text();
  if (!res.ok) {
    const message =
      data && typeof data === "object" && data.detail
        ? String(data.detail)
        : String(data || "Upload failed.").slice(0, 300);
    throw new Error(message);
  }
  return data;
}

async function selectChatFiles(event) {
  const input = $("#chat-files");
  const files = Array.from(input.files || []);
  input.value = "";
  if (!files.length) return;
  if (state.pendingAttachments.length + files.length > 6) {
    toast("You can attach up to 6 files per message.", true);
    return;
  }
  state.uploadsInProgress += files.length;
  updateAttachmentBusy();
  for (const file of files) {
    if (file.size > 20 * 1024 * 1024) {
      toast(file.name + " is too large (max 20 MB).", true);
      state.uploadsInProgress -= 1;
      updateAttachmentBusy();
      continue;
    }
    try {
      const attachment = await uploadChatFile(file);
      state.pendingAttachments.push(attachment);
      renderPendingAttachments();
    } catch (error) {
      toast(file.name + ": " + error.message, true);
    } finally {
      state.uploadsInProgress -= 1;
      updateAttachmentBusy();
    }
  }
}

function looksLikeChatOtp(text) {
  const match = /^\s*(?:(?:my|the)\s+)?(?:(?:otp|code|verification\s+code|one[-\s]?time\s+code)\s*(?:is|:|=|#)?\s*)?([A-Za-z0-9-]{4,20})\s*$/i.exec(
    String(text || "")
  );
  return Boolean(match && /\d/.test(match[1]));
}

async function chatDisplayText(text) {
  if (!looksLikeChatOtp(text)) return text;
  const session = state.chatAgentId ? "agent:" + state.chatAgentId : "default";
  try {
    const status = await api(
      "/api/browser/otp-status?session=" + encodeURIComponent(session)
    );
    if (status.required) return "[one-time code submitted privately]";
  } catch (error) {
    /* fall back to showing the original message */
  }
  return text;
}

async function sendMessage(event) {
  if (event) event.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  const attachments = state.pendingAttachments.slice();
  if (!text && !attachments.length) return;
  if (state.uploadsInProgress > 0) {
    toast("Wait for the attachments to finish uploading.", true);
    return;
  }

  if (state.chatGroup) {
    if (attachments.length) {
      toast("Attachments are only supported in direct chats.", true);
      return;
    }
    const group = state.chatGroup;
    input.value = "";
    appendMessage({ role: "user", content: text, created_at: Date.now() / 1000 });
    try {
      await api(
        "/api/team/groups/" +
          encodeURIComponent(group.agent_a_id) +
          "/" +
          encodeURIComponent(group.agent_b_id) +
          "/messages",
        { method: "POST", body: { content: text } }
      );
      await loadGroupChat();
      await refreshBadges();
    } catch (error) {
      appendMessage({
        role: "tool_note",
        content: "Error: " + error.message,
        created_at: Date.now() / 1000,
      });
      toast(error.message, true);
    }
    return;
  }

  const activeChatId = state.chatId;
  const shownText = await chatDisplayText(text);
  if (state.runs[activeChatId]) {
    input.value = "";
    appendMessage({
      role: "user",
      content: shownText,
      attachments: attachments,
      created_at: Date.now() / 1000,
    });
    try {
      const result = await api("/api/chat", {
        method: "POST",
        body: {
          message: text,
          chat_id: activeChatId,
          agent_id: state.chatAgentId || null,
          attachments: attachments,
        },
      });
      clearPendingAttachments();
      if (result.queued) {
        toast("Queued for the current run.");
        if (result.otp_handled) await loadChat();
      } else {
        await loadChat();
      }
    } catch (error) {
      appendMessage({
        role: "tool_note",
        content: "Error: " + error.message,
        created_at: Date.now() / 1000,
      });
      toast(error.message, true);
    }
    return;
  }

  const runChatId = state.chatId;
  const runAgentId = state.chatAgentId;
  if (state.runs[runChatId]) return;
  state.runs[runChatId] = { startedAt: Date.now(), agentId: runAgentId };
  input.value = "";
  appendMessage({
    role: "user",
    content: shownText,
    attachments: attachments,
    created_at: Date.now() / 1000,
  });
  appendWorkingIndicator();
  updateChatRunUI();
  renderAgentNav();
  startChatPolling();

  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: {
        message: text,
        chat_id: runChatId,
        agent_id: runAgentId || null,
        attachments: attachments,
      },
    });
    clearPendingAttachments();
    delete state.chatErrors[runChatId];
    if (state.chatId === runChatId) {
      removeWorkingIndicators();
      appendSteps(result.steps);
      appendMessage({
        role: "assistant",
        content: result.reply,
        attachments: result.images,
        created_at: Date.now() / 1000,
      });
      appendPending(result.pending_approvals);
      (result.pending_credentials || []).forEach(appendCredentialCard);
    }
    if (result.otp_handled) await loadChat();
    refreshBadges();
  } catch (error) {
    state.chatErrors[runChatId] = error.message;
    if (state.chatId === runChatId) {
      removeWorkingIndicators();
      appendMessage({
        role: "tool_note",
        content: "Error: " + error.message,
        created_at: Date.now() / 1000,
      });
      delete state.chatErrors[runChatId];
    }
    toast(error.message, true);
  } finally {
    delete state.runs[runChatId];
    updateChatRunUI();
    renderAgentNav();
    if (state.chatId === runChatId) {
      removeWorkingIndicators();
      input.focus();
      // The request can fail or be interrupted while the server keeps running.
      // Ask the server immediately rather than assuming the run is over.
      setTimeout(syncChatRunState, 300);
    }
  }
}

async function stopChat() {
  const stopButton = $("#chat-stop");
  const chatId = state.chatId;
  stopButton.disabled = true;
  stopButton.textContent = "Stopping…";
  try {
    const result = await api("/api/chat/cancel", {
      method: "POST",
      body: { chat_id: chatId },
    });
    toast(
      result.cancelled
        ? "Stopping after the current step…"
        : "Nothing was running for this chat."
    );
    if (!result.cancelled) {
      stopButton.disabled = false;
      stopButton.textContent = "Stop";
      stopButton.classList.add("hidden");
    }
  } catch (error) {
    toast(error.message, true);
    stopButton.disabled = false;
    stopButton.textContent = "Stop";
  }
}

/* --------------------------------------------------------------- tasks */
const TASK_HINTS = {
  once: { hint: "ISO date/time, e.g. 2026-12-01T09:30", placeholder: "2026-12-01T09:30", type: "datetime-local" },
  interval: { hint: "Seconds, or 30m / 2h / 1d (minimum 60 seconds)", placeholder: "2h", type: "text" },
  cron: { hint: "Five-field cron, e.g. 0 8 * * 1-5", placeholder: "0 8 * * 1-5", type: "text" },
};

function updateTaskHint() {
  const kind = $("#task-kind").value;
  const config = TASK_HINTS[kind];
  $("#task-hint").textContent = config.hint;
  const input = $("#task-schedule");
  input.placeholder = config.placeholder;
  input.type = config.type;
  input.value = "";
}

async function loadTasks() {
  try {
    const data = await api("/api/tasks");
    state.tasks = data.tasks || [];
    const activeCount = state.tasks.filter((task) => task.active).length;
    $("#task-count").textContent = activeCount ? String(activeCount) : "";
    const list = $("#task-list");
    if (!state.tasks.length) {
      list.innerHTML =
        '<div class="card muted">No tasks yet. Schedule a briefing, a reminder, or a recurring check.</div>';
      return;
    }
    list.innerHTML = state.tasks
      .map((task) => {
        const statusBadge = task.last_status
          ? '<span class="badge ' +
            (task.last_status === "ok" ? "ok" : "") +
            '">' +
            esc(task.last_status) +
            "</span>"
          : "";
        return (
          '<div class="item ' +
          (task.last_status === "error" ? "error" : "") +
          '">' +
          '<div class="grow"><h4>' +
          esc(task.title) +
          (task.active ? "" : ' <span class="badge">paused</span>') +
          "</h4>" +
          '<div class="meta">' +
          esc(task.kind) +
          " · " +
          esc(task.schedule) +
          " · " +
          esc(task.timezone) +
          " · next " +
          esc(fmtWhen(task.next_run_at)) +
          "</div>" +
          '<div class="meta">' +
          esc(task.run_count) +
          " runs · last " +
          esc(task.last_run_at ? fmtTime(task.last_run_at) : "never") +
          " " +
          statusBadge +
          "</div>" +
          (task.last_result
            ? '<pre class="detail">' + esc(task.last_result) + "</pre>"
            : "") +
          "</div>" +
          '<div class="controls">' +
          '<button data-task-action="run" data-id="' +
          task.id +
          '">Run now</button>' +
          '<button data-task-action="' +
          (task.active ? "pause" : "resume") +
          '" data-id="' +
          task.id +
          '">' +
          (task.active ? "Pause" : "Resume") +
          "</button>" +
          '<button data-task-action="runs" data-id="' +
          task.id +
          '">History</button>' +
          '<button class="danger" data-task-action="delete" data-id="' +
          task.id +
          '">Delete</button>' +
          "</div></div>"
        );
      })
      .join("");
  } catch (error) {
    toast(error.message, true);
  }
}

async function createTask(event) {
  event.preventDefault();
  const payload = {
    title: $("#task-title").value.trim(),
    prompt: $("#task-prompt").value.trim(),
    kind: $("#task-kind").value,
    schedule: $("#task-schedule").value.trim(),
    timezone: $("#task-tz").value.trim() || null,
  };
  try {
    await api("/api/tasks", { method: "POST", body: payload });
    $("#task-form").reset();
    updateTaskHint();
    toast("Task scheduled.");
    await loadTasks();
    await refreshBadges();
  } catch (error) {
    toast(error.message, true);
  }
}

async function showRuns(taskId) {
  try {
    const task = state.tasks.find((item) => String(item.id) === String(taskId));
    const data = await api("/api/tasks/" + taskId + "/runs");
    $("#runs-title").textContent =
      "Run history · " + (task ? task.title : "#" + taskId);
    $("#runs-panel").classList.remove("hidden");
    const runs = data.runs || [];
    $("#runs-list").innerHTML = runs.length
      ? runs
          .map((run) => {
            const cls =
              run.status === "ok" ? "ok" : run.status === "error" ? "error" : "";
            const duration = run.finished_at
              ? Math.max(0, Math.round(run.finished_at - run.started_at)) + "s"
              : "running";
            return (
              '<div class="run ' +
              cls +
              '"><div class="meta">' +
              esc(fmtTime(run.started_at)) +
              " · " +
              esc(run.status) +
              " · " +
              duration +
              "</div><div>" +
              esc((run.result || "").slice(0, 800)) +
              "</div></div>"
            );
          })
          .join("")
      : '<div class="muted">No runs yet.</div>';
    $("#runs-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    toast(error.message, true);
  }
}

async function onTaskAction(event) {
  const button = event.target.closest("[data-task-action]");
  if (!button) return;
  const taskId = button.dataset.id;
  const action = button.dataset.taskAction;
  try {
    if (action === "runs") {
      await showRuns(taskId);
      return;
    }
    if (action === "delete") {
      if (!window.confirm("Delete this task and its run history?")) return;
      await api("/api/tasks/" + taskId, { method: "DELETE" });
    } else {
      await api("/api/tasks/" + taskId + "/" + action, { method: "POST" });
    }
    await loadTasks();
    await refreshBadges();
  } catch (error) {
    toast(error.message, true);
  }
}

/* --------------------------------------------------------------- team */
async function loadTeam() {
  try {
    const data = await api("/api/team");
    state.team = data;
    state.agents = data.agents || [];
    renderAgentNav();
    loadGroups();
    renderTeamAgents(data.agents || []);
    renderTeamTasks(data.tasks || []);
    if (state.teamTaskId) {
      await loadTeamThread(state.teamTaskId);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadGroups() {
  try {
    const data = await api("/api/team/groups");
    state.groups = data.groups || [];
    renderAgentNav();
  } catch (error) {
    /* groups are best effort */
  }
}

function renderTeamAgents(agents) {
  const list = $("#bot-list");
  list.innerHTML = agents.length
    ? agents
        .map((agent) => {
          const paused = agent.status !== "active";
          return (
            '<div class="item"><div class="grow"><h4>#' +
            esc(agent.id) +
            " " +
            esc(agent.name) +
            ' <span class="badge">' +
            esc(agent.status) +
            "</span></h4>" +
            '<div class="meta">' +
            esc(agent.role || "No role") +
            " · " +
            esc(agent.open_tasks) +
            " open · " +
            esc(agent.done_tasks) +
            " done</div>" +
            '<pre class="detail">' +
            esc((agent.instructions || "").slice(0, 500)) +
            "</pre></div>" +
            '<div class="controls">' +
            (agent.status === "active"
              ? '<button class="ghost" data-bot-action="chat" data-id="' +
                esc(agent.id) +
                '">Chat</button>'
              : "") +
            '<button class="ghost" data-bot-action="toggle" data-id="' +
            esc(agent.id) +
            '" data-status="' +
            esc(paused ? "active" : "paused") +
            '">' +
            (paused ? "Resume" : "Pause") +
            "</button>" +
            '<button class="danger" data-bot-action="archive" data-id="' +
            esc(agent.id) +
            '">Archive</button></div></div>'
          );
        })
        .join("")
    : '<div class="card muted">No bots yet. Describe one above and Muse will design its role.</div>';

  const select = $("#team-agent");
  const current = select.value;
  const active = agents.filter((agent) => agent.status === "active");
  select.innerHTML = active.length
    ? active
        .map(
          (agent) =>
            '<option value="' +
            esc(agent.id) +
            '">#' +
            esc(agent.id) +
            " · " +
            esc(agent.name) +
            "</option>"
        )
        .join("")
    : '<option value="">Create a bot first</option>';
  if (current && active.some((agent) => String(agent.id) === current)) {
    select.value = current;
  }
}

function renderTeamTasks(tasks) {
  const list = $("#team-task-list");
  if (!tasks.length) {
    list.innerHTML =
      '<div class="card muted">No team tasks yet. Assign one above.</div>';
    return;
  }
  list.innerHTML = tasks
    .map((task) => {
      const status = esc(task.status);
      const canRun = task.status === "queued" || task.status === "needs_approval";
      const canCancel = canRun;
      return (
        '<div class="item ' +
        (task.status === "failed" ? "error" : "") +
        '"><div class="grow"><h4>#' +
        esc(task.id) +
        " " +
        esc(task.title) +
        ' <span class="badge">' +
        status +
        "</span></h4>" +
        '<div class="meta">' +
        esc(task.agent_name || "Unassigned") +
        " · " +
        esc(fmtTime(task.created_at)) +
        (task.parent_task_id ? " · parent #" + esc(task.parent_task_id) : "") +
        "</div>" +
        (task.result
          ? '<pre class="detail">' + esc(String(task.result).slice(0, 900)) + "</pre>"
          : task.error
          ? '<pre class="detail">' + esc(String(task.error).slice(0, 900)) + "</pre>"
          : "") +
        "</div><div class=\"controls\">" +
        '<button class="ghost" data-team-open="' +
        esc(task.id) +
        '">Open</button>' +
        (canRun
          ? '<button class="primary" data-team-run="' +
            esc(task.id) +
            '">Run now</button>'
          : "") +
        (canCancel
          ? '<button class="danger" data-team-cancel="' +
            esc(task.id) +
            '">Cancel</button>'
          : "") +
        "</div></div>"
      );
    })
    .join("");
}

async function createBot(event) {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  const brief = $("#bot-brief").value.trim();
  const name = $("#bot-name").value.trim();
  if (!brief) return;
  button.disabled = true;
  button.textContent = "Designing…";
  try {
    const result = await api("/api/team/agents", {
      method: "POST",
      body: { brief: brief, name: name || null },
    });
    $("#bot-brief").value = "";
    $("#bot-name").value = "";
    toast("Created " + result.agent.name + ".");
    await loadTeam();
    await refreshBadges();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Create bot";
  }
}

async function createTeamTask(event) {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  const agentId = Number($("#team-agent").value);
  const title = $("#team-task-title").value.trim();
  const prompt = $("#team-task-prompt").value.trim();
  if (!agentId || !title || !prompt) {
    toast("Choose a bot and fill in the task.", true);
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/team/tasks", {
      method: "POST",
      body: { agent_id: agentId, title: title, prompt: prompt },
    });
    $("#team-task-title").value = "";
    $("#team-task-prompt").value = "";
    toast("Task #" + result.task.id + " queued.");
    await loadTeam();
    await refreshBadges();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function onTeamAction(event) {
  const botButton = event.target.closest("[data-bot-action]");
  if (botButton) {
    const id = botButton.dataset.id;
    const action = botButton.dataset.botAction;
    if (action === "chat") {
      state.chatAgentId = Number(id);
      state.chatId = "agent:" + id + ":dm";
      updateChatHeader();
      switchTab("chat");
      return;
    }
    botButton.disabled = true;
    try {
      if (action === "archive") {
        if (!window.confirm("Archive this bot? Its queued tasks will be cancelled.")) {
          botButton.disabled = false;
          return;
        }
        await api("/api/team/agents/" + id, { method: "DELETE" });
        toast("Bot archived.");
      } else {
        await api("/api/team/agents/" + id, {
          method: "PATCH",
          body: { status: botButton.dataset.status },
        });
        toast(botButton.dataset.status === "active" ? "Bot resumed." : "Bot paused.");
      }
      await loadTeam();
      await refreshBadges();
    } catch (error) {
      toast(error.message, true);
      botButton.disabled = false;
    }
    return;
  }

  const runButton = event.target.closest("[data-team-run]");
  if (runButton) {
    const id = runButton.dataset.teamRun;
    runButton.disabled = true;
    runButton.textContent = "Running…";
    try {
      await api("/api/team/tasks/" + id + "/run", { method: "POST" });
      toast("Task #" + id + " finished.");
      await loadTeam();
      await loadTeamThread(id);
      await refreshBadges();
    } catch (error) {
      toast(error.message, true);
    } finally {
      runButton.disabled = false;
      runButton.textContent = "Run now";
    }
    return;
  }

  const cancelButton = event.target.closest("[data-team-cancel]");
  if (cancelButton) {
    const id = cancelButton.dataset.teamCancel;
    try {
      await api("/api/team/tasks/" + id + "/cancel", { method: "POST" });
      toast("Task cancelled.");
      await loadTeam();
      await refreshBadges();
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }

  const openButton = event.target.closest("[data-team-open]");
  if (openButton) {
    await loadTeamThread(openButton.dataset.teamOpen);
  }
}

async function loadTeamThread(taskId) {
  try {
    const data = await api("/api/team/tasks/" + taskId);
    state.teamTaskId = Number(taskId);
    const task = data.task;
    $("#team-thread").classList.remove("hidden");
    $("#team-thread-title").textContent =
      "#" + task.id + " " + task.title + " · " + (task.agent_name || "unassigned");
    const parts = [];
    parts.push(
      '<div class="item"><div class="grow"><h4>Task <span class="badge">' +
        esc(task.status) +
        "</span></h4>" +
        '<div class="meta">Created ' +
        esc(fmtTime(task.created_at)) +
        (task.parent_task_id ? " · parent #" + esc(task.parent_task_id) : "") +
        "</div><div class=\"md\">" +
        renderMarkdown(task.prompt) +
        "</div>" +
        (task.result
          ? '<div class="md" style="margin-top:9px">' +
            renderMarkdown(task.result) +
            "</div>"
          : task.error
          ? '<pre class="detail">Error: ' + esc(task.error) + "</pre>"
          : "") +
        "</div></div>"
    );
    (data.messages || []).forEach((message) => {
      parts.push(
        '<div class="item"><div class="grow"><h4>' +
          esc(message.from_name || "User") +
          " → " +
          esc(message.to_name || "Agent") +
          ' <span class="badge">' +
          esc(message.kind) +
          "</span></h4>" +
          '<div class="meta">' +
          esc(fmtTime(message.created_at)) +
          '</div><div class="md">' +
          renderMarkdown(message.content) +
          "</div></div></div>"
      );
    });
    $("#team-thread-body").innerHTML = parts.join("");
  } catch (error) {
    toast(error.message, true);
  }
}


/* --------------------------------------------------------------- vault */
async function loadVault() {
  try {
    const data = await api("/api/vault/entries");
    state.vault = data;
    renderVault(data.status, data.entries || []);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderVault(status, entries) {
  const gate = $("#vault-gate");
  const body = $("#vault-body");
  const timer = $("#vault-timer");
  if (!status.initialized) {
    timer.textContent = "not initialized";
    body.classList.add("hidden");
    gate.innerHTML =
      '<form id="vault-init" class="card form-grid">' +
      '<div class="wide"><h3>Create your vault</h3><p class="muted small">' +
      "Choose a master password. It is never stored and cannot be recovered. " +
      "If you lose it, entries stay unreadable.</p></div>" +
      '<label>Master password<input id="vault-new" type="password" minlength="10" required autocomplete="new-password"></label>' +
      '<label>Confirm<input id="vault-confirm" type="password" minlength="10" required autocomplete="new-password"></label>' +
      '<div class="wide actions"><span class="muted small">Argon2id (64 MiB) + AES-256-GCM.</span>' +
      '<button class="primary" type="submit">Create vault</button></div></form>';
    $("#vault-init").addEventListener("submit", initVault);
    return;
  }
  if (!status.unlocked) {
    timer.textContent = "locked";
    body.classList.add("hidden");
    gate.innerHTML =
      '<form id="vault-unlock" class="card form-grid">' +
      '<div class="wide"><h3>Vault locked</h3><p class="muted small">Unlock to add, ' +
      "reveal or autofill credentials. It locks again after " +
      Math.round(status.idle_lock_seconds / 60) +
      " minutes of inactivity.</p></div>" +
      '<label>Master password<input id="vault-pass" type="password" required autocomplete="current-password"></label>' +
      '<div class="wide actions"><span></span><button class="primary" type="submit">Unlock</button></div></form>';
    $("#vault-unlock").addEventListener("submit", unlockVault);
    return;
  }
  gate.innerHTML = "";
  body.classList.remove("hidden");
  timer.textContent = status.seconds_until_lock
    ? "locks in " + Math.ceil(status.seconds_until_lock / 60) + " min"
    : "unlocked";
  renderEntries(entries);
}

function renderEntries(entries) {
  const list = $("#vault-list");
  if (!entries.length) {
    list.innerHTML =
      '<div class="card muted">No entries yet. Add one above. Secrets are encrypted before they touch disk.</div>';
    return;
  }
  list.innerHTML = entries
    .map(
      (entry) =>
        '<div class="item"><div class="grow"><h4>' +
        esc(entry.label) +
        '</h4><div class="meta">' +
        esc((entry.domains || []).join(", ")) +
        " · updated " +
        esc(fmtTime(entry.updated_at)) +
        '</div><div class="secret-slot" id="secret-' +
        entry.id +
        '"></div></div><div class="controls">' +
        '<button data-vault-action="reveal" data-id="' +
        entry.id +
        '">Reveal</button>' +
        '<button data-vault-action="totp" data-id="' +
        entry.id +
        '">Set 2FA</button>' +
        '<button class="danger" data-vault-action="delete" data-id="' +
        entry.id +
        '">Delete</button></div></div>'
    )
    .join("");
}

async function initVault(event) {
  event.preventDefault();
  if ($("#vault-new").value !== $("#vault-confirm").value) {
    toast("Passwords do not match.", true);
    return;
  }
  try {
    await api("/api/vault/initialize", {
      method: "POST",
      body: { master_password: $("#vault-new").value },
    });
    toast("Vault created. Back up data/ and never lose this password.");
    await loadVault();
  } catch (error) {
    toast(error.message, true);
  }
}

async function unlockVault(event) {
  event.preventDefault();
  try {
    await api("/api/vault/unlock", {
      method: "POST",
      body: { master_password: $("#vault-pass").value },
    });
    await loadVault();
  } catch (error) {
    toast(error.message, true);
  }
}

async function addEntry(event) {
  event.preventDefault();
  const domains = $("#entry-domains")
    .value.split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  try {
    await api("/api/vault/entries", {
      method: "POST",
      body: {
        label: $("#entry-label").value.trim(),
        domains: domains,
        username: $("#entry-username").value,
        password: $("#entry-password").value,
        totp_secret: $("#entry-totp").value.trim(),
        url: $("#entry-url").value.trim(),
        notes: $("#entry-notes").value,
      },
    });
    $("#entry-form").reset();
    toast("Entry saved.");
    await loadVault();
  } catch (error) {
    toast(error.message, true);
  }
}

async function revealSecret(entryId) {
  try {
    const data = await api("/api/vault/entries/" + entryId + "/reveal", {
      method: "POST",
    });
    const secret = data.secret;
    const slot = $("#secret-" + entryId);
    if (!slot) return;
    slot.innerHTML =
      '<div class="secret-grid"><span>Username</span><code class="copyable">' +
      esc(secret.username) +
      '</code><span>Password</span><span class="secret-password-box"></span><span>URL</span><code>' +
      esc(secret.url) +
      '</code><span>Notes</span><code>' +
      esc(secret.notes) +
      '</code></div><p class="muted small">Password is hidden by default. Use Show or Copy. The reveal was written to the audit log.</p>';

    const passwordBox = slot.querySelector(".secret-password-box");
    const passwordValue = String(secret.password || "");
    if (passwordBox) {
      const input = document.createElement("input");
      input.type = "password";
      input.readOnly = true;
      input.className = "secret-password";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = "••••••••";
      input.setAttribute("aria-label", "Password");
      const show = document.createElement("button");
      show.type = "button";
      show.className = "link";
      show.textContent = "Show";
      show.disabled = !passwordValue;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "link";
      copy.textContent = "Copy";
      copy.disabled = !passwordValue;
      show.addEventListener("click", () => {
        const hidden = input.type === "password";
        if (hidden) {
          input.value = passwordValue;
          input.type = "text";
        } else {
          input.value = "";
          input.type = "password";
        }
        show.textContent = hidden ? "Hide" : "Show";
      });
      copy.addEventListener("click", () => {
        if (navigator.clipboard && passwordValue) {
          navigator.clipboard
            .writeText(passwordValue)
            .then(() => toast("Password copied."))
            .catch(() => toast("Could not copy the password.", true));
        }
      });
      passwordBox.appendChild(input);
      passwordBox.appendChild(show);
      passwordBox.appendChild(copy);
    }

    slot.querySelectorAll("code.copyable").forEach((code) => {
      code.addEventListener("click", () => {
        if (navigator.clipboard) {
          navigator.clipboard
            .writeText(code.textContent)
            .then(() => toast("Copied."))
            .catch(() => {});
        }
      });
    });
  } catch (error) {
    toast(error.message, true);
  }
}

async function onVaultAction(event) {
  const button = event.target.closest("[data-vault-action]");
  if (!button) return;
  const entryId = button.dataset.id;
  if (button.dataset.vaultAction === "reveal") {
    await revealSecret(entryId);
    return;
  }
  if (button.dataset.vaultAction === "totp") {
    const secret = window.prompt(
      "Base32 TOTP secret or otpauth:// URI. Leave blank to remove 2FA."
    );
    if (secret === null) return;
    try {
      await api("/api/vault/entries/" + entryId, {
        method: "PUT",
        body: { totp_secret: secret.trim() },
      });
      toast(secret.trim() ? "Two-factor secret saved." : "Two-factor secret removed.");
      await loadVault();
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  if (!window.confirm("Delete this credential? This cannot be undone.")) return;
  try {
    await api("/api/vault/entries/" + entryId, { method: "DELETE" });
    toast("Entry deleted.");
    await loadVault();
  } catch (error) {
    toast(error.message, true);
  }
}

async function lockVault() {
  try {
    await api("/api/vault/lock", { method: "POST" });
    await loadVault();
  } catch (error) {
    toast(error.message, true);
  }
}

async function changeMasterPassword(event) {
  event.preventDefault();
  try {
    await api("/api/vault/change-password", {
      method: "POST",
      body: {
        old_password: $("#change-old").value,
        new_password: $("#change-new").value,
      },
    });
    $("#change-form").reset();
    toast("Master password rotated. All entries were re-encrypted.");
    await loadVault();
  } catch (error) {
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------- browser */
function browserSessionForChat() {
  return state.chatAgentId ? "agent:" + state.chatAgentId : "default";
}

function prepareBrowserContext() {
  state.browserSession = browserSessionForChat();
  const owner = browserSessionLabel(state.browserSession);
  const title = $("#browser-title");
  const subtitle = $("#browser-subtitle");
  const badge = $("#browser-owner");
  if (title) title.textContent = owner + "'s browser";
  if (subtitle) {
    subtitle.textContent =
      "An isolated Chromium window for " + owner + ". Other agents use different windows.";
  }
  if (badge) badge.textContent = owner;
  const empty = $("#browser-empty");
  if (empty) {
    empty.textContent =
      "When " + owner + " opens a page, its isolated browser window appears here.";
  }
}

async function loadBrowser() {
  prepareBrowserContext();
  await refreshBrowserStatus();
  if (state.browser && state.browser.started) {
    await focusBrowserIfNeeded(false);
    await refreshBrowserStatus();
  }
  startBrowserPolling();
}

async function focusBrowserIfNeeded(silent = true) {
  if (!state.browser || !state.browser.started) return;
  if (state.browser.active_session === state.browserSession) return;
  await activateBrowserWindow(silent);
  await refreshBrowserStatus();
}

async function activateBrowserWindow(silent = false) {
  try {
    await api("/api/browser/select", {
      method: "POST",
      body: { session: state.browserSession || "default" },
    });
  } catch (error) {
    if (!silent) toast(error.message, true);
  }
}

async function refreshBrowserStatus() {
  try {
    const session = state.browserSession || "default";
    const status = await api(
      "/api/browser/status?session=" + encodeURIComponent(session)
    );
    state.browser = status;
    const owner = browserSessionLabel(session);
    const live = $("#browser-live");
    const liveLink = $("#browser-live-link");
    const image = $("#browser-shot");
    const empty = $("#browser-empty");
    const activeHere = status.active_session === session;
    const showLive = Boolean(status.view_url && status.started && activeHere);
    const showShot = Boolean(!status.view_url && status.started && activeHere);
    const ownerBadge = $("#browser-owner");
    if (ownerBadge) ownerBadge.textContent = owner;
    $("#browser-state").textContent = !status.enabled
      ? "disabled"
      : status.started
      ? activeHere
        ? "live"
        : "switching"
      : "idle";
    if (showLive) {
      if (live.getAttribute("src") !== status.view_url) {
        live.src = status.view_url;
      }
      live.classList.remove("hidden");
      liveLink.href = status.view_url;
      liveLink.classList.remove("hidden");
    } else {
      live.classList.add("hidden");
      liveLink.classList.add("hidden");
    }
    const cors = $("#browser-cors");
    if (cors) {
      const domains = status.cors_domains || [];
      cors.textContent =
        status.cors_mode === "inject"
          ? "CORS inject" + (domains.length ? " · " + domains.join(", ") : "")
          : "CORS off";
    }
    $("#browser-url-label").textContent = status.url || "No page open.";
    if (showLive) {
      empty.classList.add("hidden");
      image.classList.add("hidden");
    } else if (showShot) {
      empty.classList.add("hidden");
      image.classList.remove("hidden");
    } else if (status.started) {
      empty.textContent = activeHere
        ? "Opening the live view…"
        : "Bringing " + owner + "'s window forward…";
      empty.classList.remove("hidden");
      image.classList.add("hidden");
    } else {
      empty.textContent =
        "When " +
        owner +
        " opens a page, its isolated browser window appears here.";
      empty.classList.remove("hidden");
      image.classList.add("hidden");
    }
  } catch (error) {
    /* ignore */
  }
}

function browserSessionLabel(id) {
  if (!id || id === "default") {
    return (state.session && state.session.agent_name) || "Muse";
  }
  const match = /^agent:(\d+)$/.exec(id);
  if (match) {
    const agent = (state.agents || []).find(
      (item) => Number(item.id) === Number(match[1])
    );
    return agent ? agent.name : "Agent " + match[1];
  }
  if (id.startsWith("group:")) return "Group";
  return id;
}

function refreshBrowserShot() {
  if (!state.browser || state.browser.view_url) return;
  if (!state.browser.started) return;
  $("#browser-shot").src =
    "/api/browser/screenshot?session=" +
    encodeURIComponent(state.browserSession || "default") +
    "&t=" +
    Date.now();
}

function startBrowserPolling() {
  stopBrowserPolling();
  refreshBrowserShot();
  state.browserTimer = setInterval(async () => {
    if (state.tab !== "browser") {
      stopBrowserPolling();
      return;
    }
    await refreshBrowserStatus();
    await focusBrowserIfNeeded();
    if (state.browser && state.browser.started) refreshBrowserShot();
  }, 3000);
}

function stopBrowserPolling() {
  if (state.browserTimer) {
    clearInterval(state.browserTimer);
    state.browserTimer = null;
  }
}

async function openBrowser(event) {
  event.preventDefault();
  let url = $("#browser-url").value.trim();
  if (!url) return;
  if (!/^https?:\/\//i.test(url)) url = "https://" + url;
  try {
    const result = await api("/api/browser/open", {
      method: "POST",
      body: { url: url, session: state.browserSession || "default", focus: true },
    });
    $("#browser-url-label").textContent = result.url || url;
    toast(result.title ? "Opened: " + result.title : "Page opened.");
    await refreshBrowserStatus();
    startBrowserPolling();
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadBrowserDiagnostics() {
  try {
    const data = await api(
      "/api/browser/diagnostics?session=" +
        encodeURIComponent(state.browserSession || "default")
    );
    const lines = [
      "URL: " + (data.url || "—"),
      "CORS mode: " + (data.cors_mode || "off") +
        (data.cors_domains && data.cors_domains.length
          ? " (" + data.cors_domains.join(", ") + ")"
          : ""),
      "CORS injections: " + (data.cors_injections || 0),
      "",
      "Console:",
    ];
    (data.console || []).forEach((item) => {
      lines.push(
        "[" + (item.type || "log") + "] " + (item.text || "").slice(0, 500)
      );
    });
    if (!(data.console || []).length) lines.push("(none)");
    lines.push("", "Failed requests:");
    (data.failed_requests || []).forEach((item) => {
      lines.push(
        (item.method || "GET") +
          " " +
          (item.url || "") +
          " — " +
          (item.failure || "")
      );
    });
    if (!(data.failed_requests || []).length) lines.push("(none)");
    const panel = $("#browser-diagnostics-panel");
    panel.textContent = lines.join("\n");
    panel.classList.remove("hidden");
  } catch (error) {
    toast(error.message, true);
  }
}

async function clearBrowserSite() {
  let current = "";
  try {
    current = new URL(state.browser && state.browser.url).hostname;
  } catch (error) {
    current = "";
  }
  const domain = window.prompt(
    "Clear cookies, cache, localStorage and service workers for:",
    current || "github.com"
  );
  if (!domain) return;
  try {
    const result = await api("/api/browser/clear-site", {
      method: "POST",
      body: { domain: domain, session: state.browserSession || "default" },
    });
    toast(
      "Cleared site data for " +
        result.domain +
        (result.reloaded ? " and reloaded." : ".")
    );
    await refreshBrowserStatus();
    await loadBrowserDiagnostics();
  } catch (error) {
    toast(error.message, true);
  }
}

/* ----------------------------------------------------------- approvals */
async function loadApprovals() {
  try {
    const data = await api("/api/approvals?trusted=1");
    state.approvals = data;
    const pending = data.pending || [];
    const credentialCount = (data.pending_credentials || []).length;
    const count = pending.length + credentialCount;
    $("#approval-count").textContent = count ? String(count) : "";
    $("#approval-count").classList.toggle("hidden", count === 0);
    renderCredentialRequests(data.pending_credentials || []);
    renderTrustedHosts(data.trusted_hosts || []);
    renderTrustedCommands(
      data.trusted_commands || [],
      data.trusted_shell_families || []
    );
    renderApprovals(pending, data.recent || []);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderApprovals(pending, recent) {
  $("#approval-list").innerHTML = pending.length
    ? pending
        .map((item) => {
          const host = item.host || "";
          const approveHost = host
            ? '<button class="ghost" data-approval-action="trust-host" data-id="' +
              item.id +
              '" data-host="' +
              esc(host) +
              '">Approve all on ' +
              esc(host) +
              "</button>"
            : "";
          return (
            '<div class="item pending"><div class="grow"><h4>#' +
            item.id +
            " · " +
            esc(item.summary || item.tool) +
            '</h4><div class="meta">' +
            esc(item.tool) +
            (host ? " · " + esc(host) : "") +
            " · requested " +
            esc(fmtTime(item.created_at)) +
            " · chat " +
            esc(item.chat_id) +
            '</div><pre class="detail">' +
            esc(JSON.stringify(item.args || {}, null, 2)) +
            '</pre></div><div class="controls">' +
            '<button class="primary" data-approval-action="approve" data-id="' +
            item.id +
            '">Approve once</button>' +
            approveHost +
            '<button class="danger" data-approval-action="reject" data-id="' +
            item.id +
            '">Reject</button></div></div>'
          );
        })
        .join("")
    : '<div class="card muted">No pending approvals. Consequential actions will appear here.</div>';

  $("#approval-history").innerHTML = recent.length
    ? recent
        .map(
          (item) =>
            '<div class="item"><div class="grow"><h4>#' +
            item.id +
            " · " +
            esc(item.tool) +
            ' <span class="badge">' +
            esc(item.status) +
            '</span></h4><div class="meta">' +
            esc(fmtTime(item.resolved_at || item.created_at)) +
            (item.host ? " · " + esc(item.host) : "") +
            (item.result ? " · " + esc(String(item.result).slice(0, 300)) : "") +
            "</div></div></div>"
        )
        .join("")
    : "";
}

function renderTrustedCommands(commands, families) {
  const list = $("#trusted-commands");
  if (!list) return;
  families = families || [];
  const cards = [];
  families.forEach((family) => {
    if (family !== "curl") return;
    cards.push(
      '<div class="item"><div class="grow"><h4>All curl commands</h4>' +
        '<div class="meta">Any curl variation runs without approval until you revoke this.</div></div>' +
        '<div class="controls"><button class="danger" data-command-family="curl">Revoke</button></div></div>'
    );
  });
  commands.forEach((command) => {
    cards.push(
      '<div class="item"><div class="grow"><h4>Approved shell command</h4>' +
        '<pre class="detail">' +
        esc(
          command.length > 240
            ? command.slice(0, 240) + "…"
            : command
        ) +
        '</pre><div class="meta">' +
        esc(command.length) +
        ' chars</div></div><div class="controls">' +
        '<button class="danger" data-command-revoke="' +
        esc(command) +
        '">Revoke</button></div></div>'
    );
  });
  list.innerHTML = cards.length
    ? cards.join("")
    : '<div class="card muted">No shell commands are remembered. Normal workspace commands run without approval; installs, unsafe paths, and non-GET downloads are remembered after you approve them once. Approving one curl command trusts all curl variations.</div>';
}

function renderTrustedHosts(hosts) {
  const list = $("#trusted-hosts");
  if (!list) return;
  list.innerHTML = hosts.length
    ? hosts
        .map(
          (item) =>
            '<div class="item"><div class="grow"><h4>' +
            esc(item.host) +
            '</h4><div class="meta">Auto-approves browser actions and credential fills · since ' +
            esc(fmtTime(item.created_at)) +
            '</div></div><div class="controls"><button class="danger" data-trust-revoke="' +
            esc(item.host) +
            '">Revoke</button></div></div>'
        )
        .join("")
    : '<div class="card muted">No trusted sites. Use “Approve all on …” on a browser action to add one.</div>';
}

async function onTrustedHostAction(event) {
  const button = event.target.closest("[data-trust-revoke]");
  if (!button) return;
  const host = button.dataset.trustRevoke;
  if (
    !window.confirm(
      "Revoke trust for " +
        host +
        "? Future browser actions and credential fills there will need approval again."
    )
  ) {
    return;
  }
  button.disabled = true;
  try {
    await api("/api/trusted-hosts/" + encodeURIComponent(host), {
      method: "DELETE",
    });
    toast("Revoked trust for " + host + ".");
    await loadApprovals();
    await refreshBadges();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
}

async function onTrustedCommandAction(event) {
  const familyButton = event.target.closest("[data-command-family]");
  if (familyButton) {
    const family = familyButton.dataset.commandFamily || "";
    if (!window.confirm("Revoke trust for all " + family + " commands?")) return;
    familyButton.disabled = true;
    try {
      await api("/api/trusted-commands/revoke", {
        method: "POST",
        body: { family: family },
      });
      toast("Revoked all " + family + " command approvals.");
      await loadApprovals();
      await refreshBadges();
    } catch (error) {
      toast(error.message, true);
      familyButton.disabled = false;
    }
    return;
  }
  const button = event.target.closest("[data-command-revoke]");
  if (!button) return;
  const command = button.dataset.commandRevoke || "";
  if (!window.confirm("Revoke this approved shell command?")) return;
  button.disabled = true;
  try {
    await api("/api/trusted-commands/revoke", {
      method: "POST",
      body: { command: command },
    });
    toast("Shell command approval revoked.");
    await loadApprovals();
    await refreshBadges();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
}

async function onApprovalAction(event) {
  const button = event.target.closest("[data-approval-action]");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.approvalAction;
  const card = button.closest("[data-approval-card]");
  let reason = "";
  let scope = "once";

  if (action === "reject") {
    reason = window.prompt("Reason for rejecting (optional):", "") || "";
  } else if (action === "trust-host") {
    const host = button.dataset.host || "";
    if (
      !window.confirm(
        "Approve all future browser actions and credential fills on " +
          host +
          "?\n\nYou can revoke this in Approvals → Trusted sites."
      )
    ) {
      return;
    }
    scope = "host";
  }

  button.disabled = true;
  try {
    if (action === "reject") {
      await api("/api/approvals/" + id + "/reject", {
        method: "POST",
        body: { reason: reason },
      });
    } else {
      await api("/api/approvals/" + id + "/approve", {
        method: "POST",
        body: { scope: scope },
      });
    }
    toast(
      action === "reject"
        ? "Rejected."
        : scope === "host"
        ? "Approved, trusted this host, and resumed."
        : "Approved and resumed."
    );
    await loadApprovals();
    await refreshBadges();
    if (card && state.tab === "chat") {
      await loadChat();
    }
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
}

/* ------------------------------------------------------------ activity */
async function loadActivity() {
  try {
    const data = await api("/api/activity?limit=200");
    const rows = data.audit || [];
    $("#activity-list").innerHTML = rows.length
      ? rows
          .map(
            (row) =>
              '<div class="item ' +
              (row.outcome === "error" || row.outcome === "denied" ? "error" : "") +
              '"><div class="grow"><h4>' +
              esc(row.action) +
              ' <span class="badge">' +
              esc(row.outcome) +
              '</span></h4><div class="meta">' +
              esc(row.actor) +
              " · " +
              esc(fmtTime(row.ts)) +
              "</div>" +
              (row.detail
                ? '<pre class="detail">' + esc(row.detail) + "</pre>"
                : "") +
              "</div></div>"
          )
          .join("")
      : '<div class="card muted">No activity yet.</div>';
  } catch (error) {
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------ settings */
async function loadSettings() {
  try {
    const data = await api("/api/settings");
    const prefs = data.preferences;
    $("#set-name").value = prefs.agent_name || "";
    $("#set-tz").value = prefs.timezone || "";
    $("#set-style").value = prefs.agent_style || "";
    $("#set-write").checked = Boolean(prefs.write_enabled);
    $("#set-auto").checked = Boolean(prefs.auto_approve_writes);
    $("#set-browser-auto").checked = Boolean(prefs.auto_approve_browser);
    $("#set-shell").checked = Boolean(prefs.shell_enabled);
    $("#set-models").textContent =
      "Text: " +
      data.limits.text_model +
      " · Image: " +
      data.limits.image_model +
      (data.limits.max_agent_steps > 0
        ? " · max " + data.limits.max_agent_steps + " rounds"
        : " · no fixed round limit");
    $("#version").textContent = "v" + ((state.session && state.session.version) || "");
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    agent_name: $("#set-name").value.trim(),
    agent_style: $("#set-style").value.trim(),
    timezone: $("#set-tz").value.trim(),
    write_enabled: $("#set-write").checked,
    auto_approve_writes: $("#set-auto").checked,
    auto_approve_browser: $("#set-browser-auto").checked,
    shell_enabled: $("#set-shell").checked,
  };
  try {
    const data = await api("/api/settings", { method: "PUT", body: payload });
    const prefs = data.preferences;
    if (state.session) {
      state.session.agent_name = prefs.agent_name;
      state.session.agent_style = prefs.agent_style;
      state.session.write_enabled = prefs.write_enabled;
      state.session.auto_approve_writes = prefs.auto_approve_writes;
      state.session.shell_enabled = prefs.shell_enabled;
      state.session.timezone = prefs.timezone;
    }
    updateSentinel();
    toast("Settings saved.");
  } catch (error) {
    toast(error.message, true);
  }
}

/* --------------------------------------------------------- credentials */
async function updateCredentialVaultHint(card) {
  const hint = card.querySelector(".vault-hint");
  if (!hint) return;
  try {
    const status = await api("/api/vault/status");
    if (!status.initialized) {
      hint.innerHTML =
        'Vault is not set up yet. <button type="button" class="link" data-open-vault>Open Vault</button>';
    } else if (!status.unlocked) {
      hint.innerHTML =
        'Vault is locked. <button type="button" class="link" data-open-vault>Unlock in Vault</button> before saving.';
    } else {
      hint.textContent =
        "Will be encrypted in your vault and bound to " +
        (card.dataset.credentialHost || "this hostname") +
        ". The model never sees it.";
    }
    const open = hint.querySelector("[data-open-vault]");
    if (open) open.addEventListener("click", () => switchTab("vault"));
  } catch (error) {
    hint.textContent = "Vault status unavailable.";
  }
}

function buildOtpCard(request) {
  const card = document.createElement("div");
  card.className = "credential-card otp-card";
  card.dataset.credentialRequest = String(request.id);
  card.dataset.credentialHost = request.domain;
  card.dataset.credentialMode = "otp";
  card.innerHTML =
    '<div class="row between wrap">' +
    "<h4>One-time code needed · " +
    esc(request.domain) +
    '</h4><span class="badge warn">OTP</span></div>' +
    '<p class="credential-reason">' +
    esc(request.reason || "Muse needs the one-time code shown by the site to continue.") +
    "</p>" +
    '<form class="credential-form">' +
    '<label class="wide">One-time code<input name="otp_code" required maxlength="20" inputmode="numeric" autocomplete="one-time-code" placeholder="123456"></label>' +
    '<p class="muted small wide">Submitted directly to the browser. It is never saved or shown to the model.</p>' +
    '<p class="credential-error wide" hidden></p>' +
    '<div class="credential-actions wide">' +
    '<button type="button" class="link" data-credential-cancel>Cancel</button>' +
    '<button type="submit" class="primary">Submit code</button>' +
    "</div></form>" +
    '<p class="credential-result" hidden></p>';
  const form = card.querySelector("form");
  form.addEventListener("submit", (event) => handleOtpSubmit(event, card, request));
  card
    .querySelector("[data-credential-cancel]")
    .addEventListener("click", () => handleCredentialCancel(card, request));
  return card;
}

function buildCredentialCard(request) {
  if (request.kind === "otp") return buildOtpCard(request);
  const card = document.createElement("div");
  const vaultOnly = !request.login_url;
  card.className = "credential-card";
  card.dataset.credentialRequest = String(request.id);
  card.dataset.credentialHost = request.domain;
  card.dataset.credentialMode = vaultOnly ? "vault" : "login";
  card.innerHTML =
    '<div class="row between wrap">' +
    "<h4>" +
    (vaultOnly ? "Vault entry needed · " : "Login needed · ") +
    esc(request.domain) +
    '</h4><span class="badge warn">vault</span></div>' +
    '<p class="credential-reason">' +
    esc(request.reason || "Muse needs a credential to continue.") +
    "</p>" +
    '<form class="credential-form">' +
    '<label>Label<input name="label" maxlength="80" required value="' +
    esc(request.label || request.domain) +
    '"></label>' +
    '<label>Saved for this host<input value="' +
    esc(request.domain) +
    '" readonly tabindex="-1"></label>' +
    '<label class="wide">Username<input name="username" maxlength="500" autocomplete="off" placeholder="you@example.com"></label>' +
    '<label class="wide">Password<input name="password" type="password" maxlength="2000" required autocomplete="new-password"></label>' +
    '<label class="wide">Two-factor secret (optional)<input name="totp_secret" type="password" maxlength="500" autocomplete="off" placeholder="Base32 secret or otpauth:// URI"></label>' +
    '<label class="wide">Notes<input name="notes" maxlength="4000" placeholder="Optional, encrypted"></label>' +
    '<label class="check wide"><input name="trust_host" type="checkbox"> Always allow browser actions on this site (including autofill)</label>' +
    '<p class="vault-hint wide">Checking vault…</p>' +
    '<p class="credential-error wide" hidden></p>' +
    '<div class="credential-actions wide">' +
    '<button type="button" class="link" data-credential-cancel>Cancel</button>' +
    '<button type="submit" class="primary">' +
    (vaultOnly ? "Save to vault" : "Save to vault &amp; sign in") +
    "</button>" +
    "</div></form>" +
    '<p class="credential-result" hidden></p>';

  const form = card.querySelector("form");
  form.addEventListener("submit", (event) =>
    handleCredentialSubmit(event, card, request)
  );
  card
    .querySelector("[data-credential-cancel]")
    .addEventListener("click", () => handleCredentialCancel(card, request));
  updateCredentialVaultHint(card);
  return card;
}

function appendCredentialCard(request) {
  if (!request) return;
  const requestId = request.id || request.request_id;
  if (!requestId) return;
  if (!request.chat_id) request.chat_id = state.chatId;
  request = Object.assign({}, request, { id: requestId });
  if (document.querySelector('[data-credential-request="' + requestId + '"]')) {
    return;
  }
  const box = $("#messages");
  if (!box) return;
  if (box.querySelector(".empty-chat")) box.innerHTML = "";
  const wrapper = document.createElement("div");
  wrapper.className = "msg credential";
  wrapper.appendChild(buildCredentialCard(request));
  box.appendChild(wrapper);
  box.scrollTop = box.scrollHeight;
}

function renderCredentialRequests(requests) {
  const list = $("#credential-list");
  if (!list) return;
  list.innerHTML = "";
  if (!requests.length) {
    list.innerHTML =
      '<div class="card muted">No pending credential cards. Muse will ask here when a login is needed.</div>';
    return;
  }
  requests.forEach((request) => list.appendChild(buildCredentialCard(request)));
}

function removeCredentialCards(requestId) {
  $$('[data-credential-request="' + requestId + '"]').forEach((card) => {
    const wrapper = card.closest(".msg.credential");
    (wrapper || card).remove();
  });
}

async function handleOtpSubmit(event, card, request) {
  event.preventDefault();
  const form = card.querySelector("form");
  const input = form.querySelector('input[name="otp_code"]');
  const button = form.querySelector('button[type="submit"]');
  const errorBox = card.querySelector(".credential-error");
  const code = input ? input.value.trim() : "";
  errorBox.hidden = true;
  if (!code) {
    errorBox.hidden = false;
    errorBox.textContent = "Enter the one-time code.";
    return;
  }
  button.disabled = true;
  button.textContent = "Submitting…";
  try {
    const result = await api(
      "/api/credential-requests/" + request.id + "/fulfill",
      { method: "POST", body: { otp_code: code } }
    );
    if (input) input.value = "";
    removeCredentialCards(request.id);
    if (state.tab === "chat" && request.chat_id === state.chatId) {
      appendMessage({
        role: "tool_note",
        content:
          "One-time code submitted for " +
          request.domain +
          ". It was not stored.",
        created_at: Date.now() / 1000,
      });
      if (result.followup) {
        appendSteps(result.followup.steps);
        appendMessage({
          role: "assistant",
          content: result.followup.reply,
          attachments: result.followup.images,
          created_at: Date.now() / 1000,
        });
        appendPending(result.followup.pending_approvals);
        (result.followup.pending_credentials || []).forEach(appendCredentialCard);
      }
      if (result.followup_error) {
        appendMessage({
          role: "tool_note",
          content: "Follow-up failed: " + result.followup_error,
          created_at: Date.now() / 1000,
        });
      }
    }
    toast("One-time code submitted.");
    await refreshBadges();
    if (state.tab === "approvals") await loadApprovals();
  } catch (error) {
    errorBox.hidden = false;
    errorBox.textContent = error.message;
    button.disabled = false;
    button.textContent = "Submit code";
  }
}

async function handleCredentialSubmit(event, card, request) {
  event.preventDefault();
  const vaultOnly = !request.login_url;
  const form = card.querySelector("form");
  const button = form.querySelector('button[type="submit"]');
  const errorBox = card.querySelector(".credential-error");
  const data = new FormData(form);
  errorBox.hidden = true;
  button.disabled = true;
  button.textContent = vaultOnly ? "Saving to vault…" : "Saving & signing in…";

  try {
    const result = await api(
      "/api/credential-requests/" + request.id + "/fulfill",
      {
        method: "POST",
        body: {
          label: String(data.get("label") || "").trim(),
          username: String(data.get("username") || ""),
          password: String(data.get("password") || ""),
          totp_secret: String(data.get("totp_secret") || ""),
          notes: String(data.get("notes") || ""),
          trust_host: Boolean(data.get("trust_host")),
        },
      }
    );
    const passwordInput = form.querySelector('input[name="password"]');
    if (passwordInput) passwordInput.value = "";
    const totpInput = form.querySelector('input[name="totp_secret"]');
    if (totpInput) totpInput.value = "";
    removeCredentialCards(request.id);

    const loginError =
      result.login && result.login.error ? result.login.error : "";
    const otpPending = result.otp_request || null;
    const note = vaultOnly
      ? "Credential saved securely to the vault for " +
        request.domain +
        ". No login was attempted."
      : result.login_ok
      ? "Credential saved to the vault and the login was submitted on " +
        request.domain +
        "."
      : otpPending
      ? "Credential saved. A one-time code is required; enter it in the card below."
      : "Credential saved to the vault, but the login failed: " +
        (loginError || "unknown error");

    if (state.tab === "chat" && request.chat_id === state.chatId) {
      appendMessage({
        role: "tool_note",
        content: note,
        created_at: Date.now() / 1000,
      });
      if (otpPending) appendCredentialCard(otpPending);
      if (result.followup) {
        appendSteps(result.followup.steps);
        appendMessage({
          role: "assistant",
          content: result.followup.reply,
          attachments: result.followup.images,
          created_at: Date.now() / 1000,
        });
        appendPending(result.followup.pending_approvals);
        (result.followup.pending_credentials || []).forEach(appendCredentialCard);
      }
      if (result.followup_error) {
        appendMessage({
          role: "tool_note",
          content: "Follow-up failed: " + result.followup_error,
          created_at: Date.now() / 1000,
        });
      }
    }

    toast(
      vaultOnly
        ? "Saved to vault."
        : result.login_ok
        ? "Signed in."
        : result.otp_request
        ? "One-time code needed."
        : "Saved, but login failed.",
      !vaultOnly && !result.login_ok && !result.otp_request
    );
    await refreshBadges();
    if (state.tab === "approvals") await loadApprovals();
    if (state.tab === "vault") await loadVault();
  } catch (error) {
    errorBox.hidden = false;
    errorBox.textContent = error.message;
    button.disabled = false;
    button.textContent = vaultOnly ? "Save to vault" : "Save to vault & sign in";
    await updateCredentialVaultHint(card);
  }
}

async function handleCredentialCancel(card, request) {
  const button = card.querySelector("[data-credential-cancel]");
  if (button) {
    button.disabled = true;
    button.textContent = "Cancelling…";
  }
  try {
    await api("/api/credential-requests/" + request.id + "/cancel", {
      method: "POST",
      body: { reason: "Cancelled by user." },
    });
    removeCredentialCards(request.id);
    if (state.tab === "chat" && request.chat_id === state.chatId) {
      appendMessage({
        role: "tool_note",
        content: "Credential request for " + request.domain + " cancelled.",
        created_at: Date.now() / 1000,
      });
    }
    toast("Credential request cancelled.");
    await refreshBadges();
    if (state.tab === "approvals") await loadApprovals();
  } catch (error) {
    toast(error.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = "Cancel";
    }
  }
}

/* ------------------------------------------------------------ provider */
const PROVIDER_PRESETS = {
  opencode_go: {
    label: "OpenCode Go",
    hint:
      "OpenCode Go subscription. Chat Completions with tool calling; text-only, so images need a separate provider.",
    base_url: "https://opencode.ai/inference/go/openai/v1",
    text_model: "deepseek-v4.1-flash",
    session_header: "x-opencode-session",
    user_agent: "Muse/0.1.0",
  },
  opencode_console: {
    label: "OpenCode Console",
    hint:
      "Pay-as-you-go Console. Same models plus image generation. Use the Console API key and base URL.",
    base_url: "https://opencode.ai/inference/openai/v1",
    text_model: "kimi-k3",
    session_header: "x-opencode-session",
    user_agent: "Muse/0.1.0",
    image_base_url: "https://opencode.ai/inference/openai/v1",
    image_model: "gpt-image-2.5-sunburst",
  },
  openai: {
    label: "OpenAI",
    hint: "Official OpenAI API. One key covers chat and image generation.",
    base_url: "https://api.openai.com/v1",
    text_model: "gpt-4o-mini",
    session_header: "",
    user_agent: "Muse/0.1.0",
    image_model: "gpt-image-1",
  },
  openrouter: {
    label: "OpenRouter",
    hint:
      "OpenRouter gateway. Use an OpenRouter model slug that supports tool calling.",
    base_url: "https://openrouter.ai/api/v1",
    text_model: "openai/gpt-4o-mini",
    session_header: "",
    user_agent: "Muse/0.1.0",
  },
  claude: {
    label: "Claude",
    hint:
      "Anthropic OpenAI-compatible endpoint. Use a Claude model enabled for your key.",
    base_url: "https://api.anthropic.com/v1",
    text_model: "claude-sonnet-4-5",
    session_header: "",
    user_agent: "Muse/0.1.0",
  },
  deepseek: {
    label: "DeepSeek",
    hint:
      "DeepSeek OpenAI-compatible API. Select a tool-capable DeepSeek model.",
    base_url: "https://api.deepseek.com",
    text_model: "deepseek-flash",
    session_header: "",
    user_agent: "Muse/0.1.0",
  },
  custom: {
    label: "Custom",
    hint:
      "Any OpenAI-compatible Chat Completions endpoint. It must support tool calling for the agent to work.",
    base_url: "",
    text_model: "",
    session_header: "",
    user_agent: "Muse/0.1.0",
  },
};

function highlightProviderPreset(name) {
  $$("#provider-presets [data-provider-preset]").forEach((button) => {
    button.classList.toggle("active", button.dataset.providerPreset === name);
  });
}

function applyProviderPreset(name) {
  const preset = PROVIDER_PRESETS[name];
  if (!preset) return;
  state.providerPreset = name;
  highlightProviderPreset(name);
  $("#provider-hint").textContent = preset.hint;
  if (name === "custom") return;

  $("#provider-base").value = preset.base_url;
  $("#provider-model").value = preset.text_model;
  $("#provider-session").value = preset.session_header;
  $("#provider-ua").value = preset.user_agent;

  // Only fill image fields when they are empty, so switching text providers
  // does not wipe an existing image configuration.
  if (preset.image_base_url && !$("#provider-image-base").value.trim()) {
    $("#provider-image-base").value = preset.image_base_url;
  }
  if (preset.image_model && !$("#provider-image-model").value.trim()) {
    $("#provider-image-model").value = preset.image_model;
  }
}

async function loadProvider() {
  try {
    const data = await api("/api/provider");
    const provider = data.provider;
    state.provider = provider;
    state.providerPreset = provider.preset;

    $("#provider-base").value = provider.base_url || "";
    $("#provider-model").value = provider.text_model || "";
    $("#provider-session").value = provider.session_header || "";
    $("#provider-ua").value = provider.user_agent || "";
    $("#provider-image-base").value = provider.image_base_url || "";
    $("#provider-image-model").value = provider.image_model || "";
    $("#provider-image-size").value = provider.image_size || "";
    $("#provider-key").value = "";
    $("#provider-image-key").value = "";
    $("#provider-clear-key").checked = false;
    $("#provider-clear-image-key").checked = false;

    $("#provider-key").placeholder = provider.api_key_error
      ? "saved key unreadable — re-enter"
      : provider.api_key_set
      ? "saved — leave blank to keep"
      : "not set";
    $("#provider-image-key").placeholder = provider.image_api_key_error
      ? "saved key unreadable — re-enter"
      : provider.image_api_key_set
      ? "saved — leave blank to keep"
      : "not set";

    highlightProviderPreset(provider.preset);
    const preset = PROVIDER_PRESETS[provider.preset];
    $("#provider-hint").textContent = provider.api_key_error
      ? "Saved key cannot be decrypted because SESSION_SECRET or APP_PASSWORD changed. Re-enter the API key."
      : preset
      ? preset.hint
      : "Custom OpenAI-compatible endpoint. It must support tool calling.";

    const status = $("#provider-status");
    if (provider.api_key_error) {
      status.textContent = "key unreadable";
      status.className = "badge warn";
    } else if (provider.configured) {
      status.textContent = "configured";
      status.className = "badge ok";
    } else {
      status.textContent = "not configured";
      status.className = "badge warn";
    }
    $("#provider-source").textContent =
      provider.source === "database"
        ? "Saved in Muse (encrypted)"
        : "Using .env / environment";
  } catch (error) {
    toast(error.message, true);
  }
}

async function saveProvider(event) {
  event.preventDefault();
  const body = {
    preset:
      state.providerPreset || (state.provider && state.provider.preset) || "custom",
    base_url: $("#provider-base").value.trim(),
    text_model: $("#provider-model").value.trim(),
    user_agent: $("#provider-ua").value.trim(),
    session_header: $("#provider-session").value.trim(),
    image_base_url: $("#provider-image-base").value.trim(),
    image_model: $("#provider-image-model").value.trim() || "gpt-image-1",
    image_size: $("#provider-image-size").value.trim() || "1024x1024",
    clear_api_key: $("#provider-clear-key").checked,
    clear_image_api_key: $("#provider-clear-image-key").checked,
  };
  const key = $("#provider-key").value.trim();
  if (key) body.api_key = key;
  const imageKey = $("#provider-image-key").value.trim();
  if (imageKey) body.image_api_key = imageKey;
  if (!body.text_model) {
    toast("Text model is required.", true);
    return;
  }

  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    await api("/api/provider", { method: "PUT", body });
    await loadProvider();
    state.session = await api("/api/session");
    toast("Provider saved. New requests use it immediately.");
  } catch (error) {
    toast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

async function testProvider() {
  const button = $("#provider-test");
  const result = $("#provider-test-result");
  button.disabled = true;
  result.classList.remove("hidden");
  result.textContent = "Testing the saved provider…";
  try {
    const data = await api("/api/provider/test", { method: "POST" });
    if (data.ok) {
      result.textContent =
        "OK · " +
        data.model +
        " · " +
        data.latency_ms +
        " ms\n" +
        (data.reply || "(empty reply)");
    } else {
      result.textContent =
        "FAILED · " +
        data.model +
        " · " +
        data.latency_ms +
        " ms\n" +
        (data.error || "Unknown error");
    }
  } catch (error) {
    result.textContent = "FAILED\n" + error.message;
  } finally {
    button.disabled = false;
  }
}

/* ---------------------------------------------------------------- wiring */
function bindEvents() {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorBox = $("#login-error");
    errorBox.textContent = "";
    try {
      await api("/api/login", {
        method: "POST",
        body: { password: $("#login-password").value },
      });
      const session = await api("/api/session");
      enterApp(session);
    } catch (error) {
      errorBox.textContent = error.message;
    }
  });

  $("#logout").addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST" });
    } catch (error) {
      /* ignore */
    }
    if (state.badgeTimer) {
      clearInterval(state.badgeTimer);
      state.badgeTimer = null;
    }
    stopBrowserPolling();
    stopVaultPolling();
    state.session = null;
    showLogin();
  });

  $$("[data-tab]").forEach((button) =>
    button.addEventListener("click", () => switchTab(button.dataset.tab))
  );
  $("#agent-list").addEventListener("click", onAgentChat);
  $("#open-team").addEventListener("click", () => switchTab("team"));
  $("#open-settings").addEventListener("click", () => switchTab("settings"));
  $("#sidebar-new-bot").addEventListener("click", () => {
    switchTab("team");
    setTimeout(() => {
      const brief = $("#bot-brief");
      if (brief) brief.focus();
    }, 60);
  });

  $("#chat-form").addEventListener("submit", sendMessage);
  $("#chat-browser").addEventListener("click", () => switchTab("browser"));
  $("#chat-attach").addEventListener("click", () => $("#chat-files").click());
  $("#chat-files").addEventListener("change", selectChatFiles);
  $("#chat-attachments").addEventListener("click", onAttachmentClick);
  $("#chat-stop").addEventListener("click", stopChat);
  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  $("#chat-clear").addEventListener("click", async () => {
    if (!window.confirm("Start a new chat and clear the transcript?")) return;
    try {
      await api("/api/chat/clear", {
        method: "POST",
        body: { chat_id: state.chatId },
      });
      await loadChat();
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("#chat-back").addEventListener("click", () => setChatTarget(null));
  $("#messages").addEventListener("click", (event) => {
    if (event.target.closest("[data-goto-approvals]")) switchTab("approvals");
  });
  $("#messages").addEventListener("click", onApprovalAction);

  $("#task-kind").addEventListener("change", updateTaskHint);
  $("#task-form").addEventListener("submit", createTask);
  $("#task-list").addEventListener("click", onTaskAction);
  $("#bot-form").addEventListener("submit", createBot);
  $("#team-task-form").addEventListener("submit", createTeamTask);
  $("#bot-list").addEventListener("click", onTeamAction);
  $("#team-task-list").addEventListener("click", onTeamAction);
  $("#team-refresh").addEventListener("click", loadTeam);
  $("#team-thread-close").addEventListener("click", () => {
    state.teamTaskId = null;
    $("#team-thread").classList.add("hidden");
  });
  $("#runs-close").addEventListener("click", () =>
    $("#runs-panel").classList.add("hidden")
  );

  $("#vault-lock").addEventListener("click", lockVault);
  $("#entry-form").addEventListener("submit", addEntry);
  $("#vault-list").addEventListener("click", onVaultAction);
  $("#change-form").addEventListener("submit", changeMasterPassword);

  $("#browser-form").addEventListener("submit", openBrowser);
  $("#browser-refresh").addEventListener("click", async () => {
    await activateBrowserWindow();
    await refreshBrowserStatus();
    refreshBrowserShot();
  });
  $("#browser-diagnostics").addEventListener("click", loadBrowserDiagnostics);
  $("#browser-clear-site").addEventListener("click", clearBrowserSite);
  $("#browser-back-chat").addEventListener("click", () => switchTab("chat"));
  $("#browser-shot").addEventListener("error", () => {
    $("#browser-shot").classList.add("hidden");
    $("#browser-empty").classList.remove("hidden");
  });

  $("#approval-list").addEventListener("click", onApprovalAction);
  $("#trusted-hosts").addEventListener("click", onTrustedHostAction);
  $("#trusted-commands").addEventListener("click", onTrustedCommandAction);
  $("#activity-refresh").addEventListener("click", loadActivity);
  $("#settings-form").addEventListener("submit", saveSettings);

  $$("#provider-presets [data-provider-preset]").forEach((button) => {
    button.addEventListener("click", () =>
      applyProviderPreset(button.dataset.providerPreset)
    );
  });
  $("#provider-form").addEventListener("submit", saveProvider);
  $("#provider-test").addEventListener("click", testProvider);
}

async function boot() {
  bindEvents();
  updateTaskHint();
  try {
    const session = await api("/api/session");
    if (session.authenticated) {
      enterApp(session);
    } else {
      showLogin();
    }
  } catch (error) {
    showLogin();
  }
}

document.addEventListener("DOMContentLoaded", boot);








