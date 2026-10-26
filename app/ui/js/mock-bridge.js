const methodNames = ["ui_ready", "app_info", "open_external", "get_overview", "get_stats", "get_live", "get_history", "get_run", "get_run_log", "start_run", "stop_run", "set_goal", "get_settings", "save_settings", "get_schedule_status", "replace_legacy_task", "pick_path", "list_ollama_models", "export_diagnostics", "check_updates", "dismiss_update", "get_legacy_install", "import_legacy_install", "complete_setup", "list_accounts", "get_invited_friends", "save_invited_friend", "remove_invited_friend", "get_referral_info", "get_account_picture", "add_account", "sign_in_again", "get_signin_status", "begin_enrollment", "get_enrollment_status", "confirm_enrollment", "cancel_signin", "rename_account", "set_account_paused", "remove_account"];
const failure = (method) => ({ ok: false, error: { code: "internal", message_key: "errors.app.internal", params: {}, detail: `Mock method ${method} has no matching response`, fields: null } });

function jsonEqual(first, second) {
    if (first === second) return true;
    if (first === null || second === null || typeof first !== "object" || typeof second !== "object") return false;
    if (Array.isArray(first) !== Array.isArray(second)) return false;
    if (Array.isArray(first)) return first.length === second.length && first.every((value, index) => jsonEqual(value, second[index]));
    const keys = Object.keys(first);
    return keys.length === Object.keys(second).length && keys.every((key) => Object.hasOwn(second, key) && jsonEqual(first[key], second[key]));
}

export function resolveEnvelope(method, answer, request = {}) {
    const choices = Array.isArray(answer) ? answer : [answer];
    const entry = choices.find((item) => {
        if (!item || typeof item !== "object") return false;
        if (!Object.hasOwn(item, "when")) return true;
        return item.when !== null && typeof item.when === "object" && !Array.isArray(item.when) && Object.entries(item.when).every(([key, value]) => Object.hasOwn(request, key) && jsonEqual(value, request[key]));
    });
    if (!entry) return failure(method);
    const response = structuredClone(entry);
    delete response.when;
    return response;
}
export async function install() {
    const query = new URLSearchParams(window.location.search);
    const fixture = query.get("mock");
    if (!fixture || window.pywebview) return false;
    if (!/^[a-z][a-z0-9_-]*\/[a-z][a-z0-9_-]*$/.test(fixture)) throw new Error("Invalid mock fixture path");
    const root = new URL("../mock/", import.meta.url);
    const load = async (path) => {
        const response = await fetch(new URL(path, root));
        if (!response.ok) throw new Error(`Mock fixture ${path}: HTTP ${response.status}`);
        return response.json();
    };
    const [defaults, chosen] = await Promise.all([load("shell/defaults.json"), load(`${fixture}.json`)]);
    const multi = ["multi-account", "multi-account-unconfirmed"].includes(chosen.scenario);
    const multiSeed = multi ? await load("accounts/multi-account.json") : null;
    const methods = {
        get_invited_friends: { ok: true, data: { invited_friends: [] } },
        get_referral_info: { ok: true, data: { link: null, link_found: false, referrals: null, friends: null, read_at: null, unreadable: "not_read" } },
        ...defaults.methods, ...(multiSeed?.methods || {}), ...chosen.methods,
    };
    const timers = new Set();
    let invitedFriends = resolveEnvelope("get_invited_friends", methods.get_invited_friends, {}).data?.invited_friends || [];
    let mockAccounts = multi ? structuredClone(methods.list_accounts.data.accounts) : null;
    let pendingEnrollment = null;
    const mockStatus = (id, state) => ({ account_id: id, state, error_code: null, message_key: `accounts.signin.${state}`, detail: null, at: "2026-10-26T15:34:00Z" });
    let enrollmentPushId = 108000;
    const emit = (data) => window.rf?.receive([{ id: ++enrollmentPushId, topic: "signin.status", data, at: data.at }]);
    const scopedRun = (id) => {
        const run = structuredClone(methods.get_run.data);
        if (id) {
            run.accounts = run.accounts.filter((account) => account.account_id === id);
            const account = run.accounts[0];
            run.result = account?.status === "skipped" ? "skipped" : "ok";
            run.result_key = `errors.run.${run.result}`;
            run.kind = run.result === "skipped" ? "check" : "run";
            run.points_earned = account?.points_earned ?? null;
        }
        return run;
    };
    function multiResponse(method, request, response) {
        if (method === "list_accounts") return { ok: true, data: { accounts: structuredClone(mockAccounts) } };
        if (["add_account", "begin_enrollment"].includes(method)) {
            const id = method === "begin_enrollment" ? request.account_id : `account-${mockAccounts.length + 1}`;
            pendingEnrollment = { id, observed_name: method === "begin_enrollment" ? mockAccounts.find(account => account.id === id)?.name : "Morgan Synthetic", confirmation_token: "00000000000000000000000000000108" };
            const timer = setTimeout(() => { timers.delete(timer); if (pendingEnrollment?.id === id) emit(mockStatus(id, "awaiting_confirmation")); }, 100);
            timers.add(timer);
            return { ok: true, data: { ...(method === "add_account" ? { account_id: id } : {}), signin: mockStatus(id, "starting") } };
        }
        if (method === "get_enrollment_status") return { ok: true, data: { account_id: request.account_id, state: pendingEnrollment?.id === request.account_id ? "awaiting_confirmation" : "not_observed", observed_name: pendingEnrollment?.observed_name ?? null, confirmation_token: pendingEnrollment?.confirmation_token ?? null, observed_at: pendingEnrollment ? "2026-10-26T15:34:00Z" : null, same_name_limit: true } };
        if (method === "confirm_enrollment") {
            if (!pendingEnrollment || request.account_id !== pendingEnrollment.id || request.confirmation_token !== pendingEnrollment.confirmation_token || request.confirmed !== true) return { ok: false, error: { code: "accounts.enrollment_expired", message_key: "errors.app.accounts_enrollment_expired", params: {}, detail: null, fields: null } };
            let account = mockAccounts.find(item => item.id === request.account_id);
            if (!account) { account = { ...mockAccounts[0], id: request.account_id, label: pendingEnrollment.observed_name, name: pendingEnrollment.observed_name, email: "morgan@example.invalid", dir: "C:\\Synthetic\\" + request.account_id, balance: null }; mockAccounts.push(account); }
            account.identity_status = "confirmed"; account.identity_confirmed_at = "2026-10-26T15:35:00Z";
            pendingEnrollment = null;
            return { ok: true, data: { account_id: account.id, identity_status: "confirmed", confirmed_at: account.identity_confirmed_at, managed: mockAccounts.length > 1, same_name_limit: true } };
        }
        if (method === "cancel_signin") { pendingEnrollment = null; emit(mockStatus(request.account_id, "cancelled")); return { ok: true, data: {} }; }
        if (method === "get_run") return { ok: true, data: scopedRun(request.account_id) };
        if (method === "get_history") {
            const { accounts, warnings, ...run } = scopedRun(request.account_id);
            run.accounts = accounts.map(({ tasks, state_before, state_after, ...account }) => ({ ...account, task_counts: Object.fromEntries(["ok", "skip", "fail", "off"].map(status => [status, tasks.filter(task => task.status === status).length])) }));
            return { ok: true, data: { runs: [run], next_before: null } };
        }
        if (method === "get_overview") {
            const data = structuredClone(defaults.methods.get_overview.data), seed = data.accounts[0], capture = data.streaks;
            data.accounts = mockAccounts.filter(account => !request.account_id || account.id === request.account_id).map((account, index) => ({ ...structuredClone(seed), account_id: account.id, label: account.label, paused: account.paused, state: index === 0 && account.id === "account-1" ? { ...seed.state, balance: 12340 } : null, state_is_today: account.id === "account-1" }));
            data.totals = request.account_id === "account-1" ? { balance: 12340, earned_today: 150, open_points: 35 } : { balance: null, earned_today: null, open_points: null };
            data.streaks = request.account_id ? (request.account_id === "account-1" ? { ...capture, account_id: request.account_id } : null) : null;
            if (!request.account_id && data.accounts.length > 1) data.streaks_by_account = Object.fromEntries(data.accounts.map(account => [account.account_id, account.account_id === "account-1" ? { ...capture, account_id: account.account_id } : null]));
            return { ok: true, data };
        }
        if (method === "get_stats") {
            const data = structuredClone(defaults.methods.get_stats.data);
            data.accounts = mockAccounts.map(account => ({ account_id: account.id, label: account.label, paused: account.paused }));
            data.account_id = request.account_id ?? null; data.range = request.range || 30;
            data.goal = null;
            if (request.account_id !== "account-1") { data.totals.week_earned = null; data.totals.month_earned = null; data.totals.earned = null; data.totals.measured_earned = 99999; }
            return { ok: true, data };
        }
        return response;
    }
    let pushesStarted = false;
    const api = Object.fromEntries(methodNames.map((method) => [method, async (request = {}) => {
        let response = resolveEnvelope(method, methods[method], request);
        if (multi) response = multiResponse(method, request, response);
        if (method === "get_invited_friends" && response.ok) response.data.invited_friends = structuredClone(invitedFriends);
        if (method === "save_invited_friend" && response.ok) {
            const valid = typeof request.name === "string" && request.name.trim().length >= 1 && request.name.trim().length <= 40 &&
                typeof request.email === "string" && request.email.trim().length <= 254 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(request.email.trim()) &&
                !/[\u0000-\u001f\u007f-\u009f]/.test(request.name + request.email) &&
                (!Object.hasOwn(request, "id") || invitedFriends.some((friend) => friend.id === request.id));
            if (!valid) response = { ok: false, error: { code: "bad_request", message_key: "errors.app.bad_request", params: {}, detail: null, fields: null } };
            else {
                const friend = request.id ? { ...invitedFriends.find((item) => item.id === request.id) } : { ...response.data.friend, id: `friend-${crypto.randomUUID().replaceAll("-", "")}` };
                Object.assign(friend, { name: request.name.trim(), email: request.email.trim() });
                invitedFriends = request.id ? invitedFriends.map((item) => item.id === request.id ? friend : item) : [...invitedFriends, friend];
                response = { ok: true, data: { friend: structuredClone(friend) } };
            }
        }
        if (method === "remove_invited_friend" && response.ok) {
            if (!invitedFriends.some((friend) => friend.id === request.id)) response = { ok: false, error: { code: "bad_request", message_key: "errors.app.bad_request", params: {}, detail: null, fields: null } };
            else {
                invitedFriends = invitedFriends.filter((friend) => friend.id !== request.id);
                response.data.invited_friends = structuredClone(invitedFriends);
            }
        }
        if (method === "app_info" && response.ok) {
            const theme = query.get("theme");
            if (["dark", "light"].includes(theme)) response.data.theme = theme;
            if (query.has("lang")) {
                response.data.lang = query.get("lang");
                const direction = query.get("lang") === "x-rtl" ? "rtl" : response.data.locales?.find((item) => item.code === query.get("lang"))?.dir;
                if (direction) response.data.dir = direction;
                else delete response.data.dir;
            }
            if (query.has("format")) {
                const format = query.get("format");
                try {
                    if (Intl.getCanonicalLocales(format).length) response.data.format_locale = format;
                } catch (error) {
                    if (!(error instanceof RangeError)) throw error;
                }
            }
            if (query.get("reduced_motion") === "1") response.data.reduced_motion = true;
        }
        if (method === "ui_ready" && !pushesStarted && response.ok) {
            pushesStarted = true;
            for (const push of chosen.pushes || []) {
                const timer = setTimeout(() => {
                    timers.delete(timer);
                    window.rf?.receive([{ topic: push.topic, data: structuredClone(push.data) }]);
                }, Math.max(0, push.after_ms));
                timers.add(timer);
            }
        }
        if (method === "save_settings" && response.ok && request.changes) {
            const current = resolveEnvelope("get_settings", methods.get_settings, {});
            if (current.ok) methods.get_settings = { ok: true, data: { ...current.data, values: structuredClone(response.data.values), schedule: structuredClone(response.data.schedule), secrets_set: structuredClone(response.data.secrets_set || {}) } };
        }
        return response;
    }]));
    window.pywebview = { api };
    window.addEventListener("pagehide", () => { for (const timer of timers) clearTimeout(timer); }, { once: true });
    window.dispatchEvent(new Event("pywebviewready"));
    return true;
}
