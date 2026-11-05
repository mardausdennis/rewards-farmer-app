// Browser-free DOM test double. Production screens and flow are imported below.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

class Element {
    constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.className = ""; this.isConnected = true; this.classList = { add: (...names) => { this.className += " " + names.join(" "); }, remove() {} }; }
    append(...children) { for (const child of children.flat(Infinity)) { if (child == null || child === false) continue; const node = child instanceof Element ? child : Object.assign(new Element("text"), { textContent: String(child) }); node.parentElement = this; this.children.push(node); } }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    contains(node) { return node === this || this.children.some(child => child.contains(node)); }
    setAttribute(key, value) { this.attrs[key] = String(value); if (key === "id") this.id = String(value); if (key === "class") this.className = String(value); if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())] = String(value); }
    removeAttribute(key) { delete this.attrs[key]; }
    getAttribute(key) { return this.attrs[key] ?? null; }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    removeEventListener(name) { delete this.listeners[name]; }
    focus() { globalThis.document.activeElement = this; }
    select() {}
    remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); }
    closest(selector) { if (selector === "details") return this.tagName === "DETAILS" ? this : this.parentElement?.closest(selector); return null; }
    matches(selector) { if (selector.startsWith("#")) return this.id === selector.slice(1); if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1)); if (selector.startsWith("[")) { const [key, value] = selector.slice(1, -1).split("="); return value ? this.getAttribute(key) === value.replaceAll('"', "") : this.getAttribute(key) !== null; } return this.tagName.toLowerCase() === selector; }
    querySelectorAll(selector) { const all = []; for (const child of this.children) { if (selector.split(",").some(part => child.matches(part.trim()))) all.push(child); all.push(...child.querySelectorAll(selector)); } return all; }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    click() { if (this.disabled) return; return this.listeners.click?.({ preventDefault() {}, target: this }); }
}
globalThis.Node = Element;
const view = new Element();
const dialogNode = new Element("dialog");
globalThis.document = { activeElement: null, getElementById: id => id === "dialog" ? dialogNode : view.querySelector("#" + id), createElement: tag => new Element(tag), createTextNode: text => Object.assign(new Element("text"), { textContent: text }), documentElement: { dir: "ltr" } };
globalThis.window = { setInterval: () => 0, clearInterval() {} };
globalThis.location = { hash: "#/accounts" };
globalThis.CSS = { escape: value => value };
const { mount } = await import(pathToFileURL(process.argv[2] + "/js/screens/accounts.js"));
const recoveryOnly = process.argv.includes("--recovery-only");
const calls = [], pushes = new Map(), storeListeners = new Map();
let recoveryPhase = "awaiting_confirmation", recoveryRows = [];
let observer, dialogResolve, lastDialog;
const accounts = [1, 2].map(n => ({ id: `account-${n}`, label: `Synthetic ${n}`, identity_status: "confirmed", identity_last_status: null }));
const data = new Map([["accounts", accounts], ["appInfo", { has_accounts: true }]]);
const op = { operation_id: "invite-" + "1".repeat(32), source_account_id: "account-2", target_account_id: "account-3", phase: "preview", link: "https://rewards.bing.com/refer?code=SYNTHETIC_SECOND", link_read_at: "2026-10-29T21:00:00Z", unreadable: "page_unknown", preview_token: "2".repeat(32), continue_token: null, referral_status: "unknown", can_resume: true, can_discard: true };
const bridge = {
    on(topic, callback) { if (!pushes.has(topic)) pushes.set(topic, new Set()); pushes.get(topic).add(callback); return () => pushes.get(topic).delete(callback); },
    async call(method, request = {}) {
        calls.push({ method, request: structuredClone(request) });
        if (method === "list_accounts") return { accounts: data.get("accounts") };
        if (method === "get_invited_friends") return { invited_friends: [] };
        if (method === "get_referral_info") return { link: null, link_found: false, referrals: null, friends: null, read_at: null, unreadable: "not_read" };
        if (method === "list_invite_operations") return { operations: recoveryOnly ? structuredClone(recoveryRows) : [] };
        if (method === "preview_invite_account") { assert.equal(request.source_account_id, "account-2"); return { invitation: structuredClone(op) }; }
        if (method === "start_invite_account") { assert.deepEqual(request, { operation_id: op.operation_id, preview_token: op.preview_token }); return { account_id: "account-3", signin: { account_id: "account-3", state: "invite_waiting", message_key: "accounts.invite.waiting", error_code: null, invitation: { ...op, phase: "invite_open", continue_token: "3".repeat(32) } } }; }
        if (method === "continue_invite_account") { assert.deepEqual(request, { operation_id: op.operation_id, continue_token: "3".repeat(32) }); return { invitation: { ...op, phase: "continue_requested" } }; }
        if (method === "get_enrollment_status") return { account_id: "account-3", state: "awaiting_confirmation", observed_name: "Synthetic New", confirmation_token: "4".repeat(32), same_name_limit: true };
        if (method === "confirm_enrollment") { assert.equal(request.confirmed, true); assert.equal(request.confirmation_token, "4".repeat(32)); data.set("accounts", [...accounts, { id: "account-3", label: "Synthetic New", identity_status: "confirmed" }]); return {}; }
        if (method === "get_signin_status") return { account_id: "account-3", state: recoveryOnly ? recoveryPhase : "verifying" };
        if (method === "cancel_signin") return {};
        if (method === "discard_invite_account") { assert.equal(request.operation_id, op.operation_id); recoveryRows = []; return { invitation: { ...op, phase: "discarded" } }; }
        if (method === "add_account") return { account_id: "account-4", signin: { account_id: "account-4", state: "starting", message_key: "accounts.signin.starting" } };
        throw Error("Unexpected method " + method);
    },
};
const component = (name, tag = "div") => ({ create(props) { const node = new Element(tag); node.props = props; if (props.id) node.id = props.id; if (props.disabled) node.disabled = true; if (props.onClick) node.addEventListener("click", props.onClick); if (name === "textField") { const input = new Element("input"); input.value = props.value; node.append(input); } if (name === "select") node.append(new Element("select")); return node; } });
const components = Object.fromEntries(["button", "textField", "select", "banner", "spinner", "emptyState", "icon"].map(name => [name, component(name, name === "button" ? "button" : "div")]));
components.dispose = () => {};
components.accountRow = { create() { const node = new Element(); const name = new Element(); name.className = "account-row-name"; node.append(name); return node; } };
components.toast = { show() {} };
components.dialog = { open(props) { lastDialog = props; return new Promise(resolve => { dialogResolve = resolve; }); } };
const ctx = { bridge, components, i18n: { t: key => key, formatDate: () => "date", formatTime: () => "time" }, store: { get: key => data.get(key), set(key, value) { data.set(key, value); for (const callback of storeListeners.get(key) || []) callback(value); }, on(key, callback) { if (!storeListeners.has(key)) storeListeners.set(key, new Set()); storeListeners.get(key).add(callback); return () => storeListeners.get(key).delete(callback); }, update(fn) { observer = fn; return () => {}; } }, setTitle() {} };
const tick = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); };
if (recoveryOnly) {
    recoveryRows = [{ ...op, phase: "awaiting_confirmation", can_resume: false, can_discard: true }];
    const pendingScreen = await mount(view, ctx); await tick();
    assert(!view.querySelector(`#accounts-invite-resume-${op.operation_id}`));
    assert(view.querySelector(`#accounts-invite-confirm-${op.operation_id}`));
    assert.equal(calls.filter(call => call.method === "preview_invite_account").length, 0);
    view.querySelector(`#accounts-invite-confirm-${op.operation_id}`).click(); await tick();
    assert.equal(lastDialog.params.name, "Synthetic New");
    dialogResolve("cancel"); await tick();
    assert.equal(calls.filter(call => call.method === "cancel_signin").length, 1);
    assert.equal(calls.filter(call => call.method === "confirm_enrollment").length, 0);
    pendingScreen.unmount(); view.replaceChildren(); ctx.store.set("signin", null);
    recoveryRows = [{ ...op, phase: "interrupted", can_resume: true, can_discard: true, recovery_only: false }];
    const resumedScreen = await mount(view, ctx); await tick();
    await view.querySelector(`#accounts-invite-resume-${op.operation_id}`).click(); await tick();
    assert.equal(calls.at(-1).method, "preview_invite_account");
    assert.deepEqual(calls.at(-1).request, { source_account_id: "account-2", operation_id: op.operation_id });
    assert(view.querySelector("#accounts-invite-link"));
    assert.equal(calls.filter(call => call.method === "start_invite_account").length, 0);
    resumedScreen.unmount(); view.replaceChildren(); ctx.store.set("signin", null);
    recoveryRows = [{ ...op, phase: "interrupted", can_resume: false, can_discard: true, recovery_only: true, source_account_id: null, target_account_id: null }];
    const forgottenScreen = await mount(view, ctx); await tick();
    assert(!view.querySelector(`#accounts-invite-resume-${op.operation_id}`));
    const discarding = view.querySelector(`#accounts-invite-discard-${op.operation_id}`).click(); await tick();
    assert.equal(lastDialog.bodyKey, "accounts.invite.forget_body");
    dialogResolve("discard"); await discarding; await tick();
    assert.equal(calls.filter(call => call.method === "discard_invite_account").length, 1);
    assert(!view.querySelector(`#accounts-invite-discard-${op.operation_id}`));
    assert.equal(data.get("accounts").length, 2);
    forgottenScreen.unmount();
    console.log(JSON.stringify({ synthetic: true, pending_not_resumed: true, cancelled_pending: true, explicit_resume: true, metadata_only_forget: true }));
    process.exit(0);
}
const screen = await mount(view, ctx);
const root = view.children[0];
await tick();
assert(root.querySelector("#accounts-add-another"));
await root.querySelector("#accounts-add-another").click(); await tick();
const picker = root.querySelector("#accounts-invite-source");
assert.equal(picker.props.value, ""); assert.equal(picker.props.options.length, 3);
picker.props.onChange("account-2");
await root.querySelector("#accounts-invite-preview").click(); await tick();
assert.equal(root.querySelector("#accounts-invite-link").querySelector("input").value, op.link);
assert(root.querySelector("#accounts-invite-open"));
assert.equal(calls.filter(call => call.method === "start_invite_account").length, 0);
assert.equal(calls.filter(call => call.method === "add_account").length, 0);
await root.querySelector("#accounts-invite-open").click(); await tick();
assert(root.querySelector("#accounts-invite-continue"));
assert.equal(data.get("accounts").length, 2);
const continueButton = root.querySelector("#accounts-invite-continue");
const first = continueButton.click(); continueButton.click(); await first; await tick();
assert.equal(calls.filter(call => call.method === "continue_invite_account").length, 1);
assert.equal(root.querySelector("#accounts-invite-continue").disabled, true);
ctx.store.set("signin", { account_id: "account-3", state: "awaiting_confirmation", error_code: null });
await tick();
assert.equal(lastDialog.params.name, "Synthetic New");
assert.equal(lastDialog.bodyKey, "accounts.enrollment.same_name_limit");
assert.equal(data.get("accounts").length, 2);
dialogResolve("confirm"); await tick();
assert.equal(data.get("accounts").length, 3);
assert.equal(calls.filter(call => call.method === "confirm_enrollment").length, 1);
assert.equal(calls.filter(call => call.method === "start_run").length, 0);
screen.unmount();
// The ordinary no-invitation path still dispatches the original method.
ctx.store.set("signin", null);
const secondView = new Element();
const secondScreen = await mount(secondView, ctx);
await tick();
await secondView.querySelector("#accounts-add-another").click(); await tick();
await secondView.querySelector("#accounts-invite-normal").click(); await tick();
assert.equal(calls.filter(call => call.method === "add_account").length, 1);
assert.deepEqual(calls.find(call => call.method === "add_account").request, {});
secondScreen.unmount();
console.log(JSON.stringify({ synthetic: true, invite_first: true, explicit_source: true, once_continue: true, final_confirmation: true, no_earning: true }));
