import { el } from "../dom.js";
import { activeStates, statusKeys, signinFlow } from "./accounts/signin.js";
import { invitations } from "./accounts/invitations.js";

export async function mount(view, ctx) {
    const { bridge, i18n, store, router, components: c } = ctx;
    const t = i18n.t;
    let alive = true;
    let accounts = store.get("accounts") || [];
    let loaded = false;
    let error = null;
    let warnings = [];
    let busyId = null;
    let confirming = false;
    let editId = null;
    let label = "";
    let inviteChoice = false, inviteSource = "", invitePreview = null, inviteOperations = [], invitePending = false;
    const openActions = new Set();
    const root = el("section", { class: "screen accounts-screen", "data-screen": "accounts" });
    view.append(root);
    const flow = signinFlow(ctx, { accepts: (id) => accounts.some((a) => a.id === id) || flow.status?.account_id === id, changed: render, connected: refresh, awaiting: enrollment });
    const invited = invitations(ctx, render);
    const button = (id, props) => { const node = c.button.create(props); node.id = id; return node; };
    const keyId = (account) => encodeURIComponent(account.id);
    function disclosure(labelKey, children, id, open = false) {
        const summary = el("summary", { class: "log-toggle" }, [el("span", { text: t(labelKey) }), c.icon.create({ name: open ? "chevron-up" : "chevron-down" })]);
        const node = el("details", { class: "accounts-disclosure", open }, [summary, el("div", { class: "accounts-disclosure-body" }, children)]);
        node.addEventListener("toggle", () => { summary.replaceChild(c.icon.create({ name: node.open ? "chevron-up" : "chevron-down" }), summary.lastElementChild); if (id && node.isConnected) { if (node.open) openActions.add(id); else openActions.delete(id); } });
        return node;
    }
    function sync(next) {
        accounts = next;
        store.set("accounts", next);
        store.set("appInfo", { ...store.get("appInfo"), has_accounts: next.length > 0 });
    }
    async function refresh() {
        const invitationLoad = invited.load();
        try {
            const data = await bridge.call("list_accounts");
            if (alive) { sync(data.accounts); loaded = true; }
        } catch (err) { if (alive) error = err; }
        await invitationLoad;
        try {
            const data = await bridge.call("list_invite_operations");
            if (alive) inviteOperations = data.operations;
        } catch (err) { if (alive) error = err; }
        render();
    }
    async function action(account, method, request, successKey) {
        if (busyId || confirming) return;
        busyId = account.id;
        error = null;
        warnings = [];
        render();
        try {
            const data = await bridge.call(method, request);
            if (!alive) return;
            if (data.account) sync(accounts.map((old) => old.id === data.account.id ? data.account : old));
            if (method === "rename_account") editId = null;
            if (successKey) c.toast.show({ tone: "success", messageKey: successKey });
        } catch (err) { if (alive) error = err; }
        finally { busyId = null; render(); }
    }
    async function signin(account) {
        if (!account) return;
        error = null;
        editId = null;
        try { await flow.start("sign_in_again", { account_id: account.id }); } catch { /* The sign-in panel shows the envelope. */ }
    }
    async function enrollment(status) {
        if (confirming || !alive) return;
        confirming = true;
        let dismissed = false;
        render();
        try {
            const observed = await bridge.call("get_enrollment_status", { account_id: status.account_id });
            if (!alive) return;
            if (observed.account_id !== status.account_id || observed.state !== "awaiting_confirmation" || typeof observed.observed_name !== "string" || !observed.observed_name.trim() || typeof observed.confirmation_token !== "string" || !observed.confirmation_token) throw { messageKey: "errors.app.accounts_identity_unreadable" };
            const dialog = document.getElementById("dialog");
            const dismiss = (event) => { if (event.type === "cancel" || event.key === "Escape") dismissed = true; };
            dialog.addEventListener("keydown", dismiss, true);
            dialog.addEventListener("cancel", dismiss, true);
            let choice;
            try {
                choice = await c.dialog.open({ titleKey: "accounts.enrollment.observed_name", bodyKey: "accounts.enrollment.same_name_limit", params: { name: observed.observed_name }, actions: [
                    { id: "cancel", labelKey: "accounts.enrollment.cancel", variant: "ghost" },
                    { id: "confirm", labelKey: "accounts.enrollment.confirm", variant: "primary" },
                ] });
            } finally {
                dialog.removeEventListener("keydown", dismiss, true);
                dialog.removeEventListener("cancel", dismiss, true);
            }
            if (!alive) return;
            if (choice !== "confirm") { if (!dismissed) await flow.cancel(); return; }
            await bridge.call("confirm_enrollment", { account_id: status.account_id, confirmation_token: observed.confirmation_token, confirmed: true });
            if (!alive) return;
            flow.confirmed();
            error = null;
            await refresh();
        } catch (err) { if (alive) error = err; }
        finally { confirming = false; render(dismissed ? "accounts-confirm-enrollment" : undefined); }
    }
    async function beginEnrollment(account) {
        if (confirming || flow.pending || activeStates.has(flow.status?.state)) return;
        error = null;
        try { await flow.start("begin_enrollment", { account_id: account.id }); } catch { /* The sign-in panel owns the error. */ }
    }
    async function addAccount(confirmMigration = false, normal = false) {
        if (busyId || confirming || flow.pending || activeStates.has(flow.status?.state)) return;
        if (flow.status?.state === "awaiting_confirmation") { await enrollment(flow.status); return; }
        const unconfirmed = accounts.find((account) => account.identity_status !== "confirmed");
        if (unconfirmed) { await beginEnrollment(unconfirmed); return; }
        if (!confirmMigration && !normal) {
            inviteChoice = true;
            inviteSource = "";
            invitePreview = null;
            render("accounts-invite-source");
            return;
        }
        error = null;
        try { await flow.start("add_account", confirmMigration ? { confirm_migration: true } : {}); }
        catch (err) {
            if (!alive) return;
            if (err.code === "accounts.migration_needed" && !confirmMigration) {
                const choice = await c.dialog.open({ titleKey: "accounts.migration.title", bodyKey: "errors.app.accounts_migration_needed", actions: [{ id: "cancel", labelKey: "common.action.cancel", variant: "ghost" }, { id: "confirm", labelKey: "accounts.migration.confirm", variant: "primary" }] });
                if (alive && choice === "confirm") await addAccount(true);
            } else if (err.code === "accounts.enrollment_required") {
                await refresh();
                const first = accounts.find((account) => account.identity_status !== "confirmed");
                if (first) await beginEnrollment(first);
            }
        }
    }
    async function previewInvitation(operation = null) {
        if (invitePending || !inviteSource && !operation) return;
        invitePending = true;
        error = null;
        render();
        try {
            const data = await bridge.call("preview_invite_account", { source_account_id: operation?.source_account_id || inviteSource, ...(operation ? { operation_id: operation.operation_id } : {}) });
            if (alive) { invitePreview = data.invitation; inviteChoice = true; }
        } catch (err) { if (alive) error = err; }
        finally { invitePending = false; render("accounts-invite-open"); }
    }
    async function startInvitation() {
        if (invitePending || !invitePreview) return;
        invitePending = true;
        render();
        try {
            await flow.start("start_invite_account", { operation_id: invitePreview.operation_id, preview_token: invitePreview.preview_token });
            if (alive) { inviteChoice = false; invitePreview = null; }
        } catch (err) { if (alive) error = err; }
        finally { invitePending = false; if (alive) { await refresh(); render(); } }
    }
    async function discardInvitation(operation) {
        const choice = await c.dialog.open({ titleKey: "accounts.invite.discard", bodyKey: operation.recovery_only ? "accounts.invite.forget_body" : "accounts.invite.discard_body", actions: [{ id: "cancel", labelKey: "common.action.cancel", variant: "ghost" }, { id: "discard", labelKey: "accounts.invite.discard", variant: "danger" }] });
        if (!alive || choice !== "discard") return;
        try {
            await bridge.call("discard_invite_account", { operation_id: operation.operation_id });
            invitePreview = null;
            inviteChoice = false;
            await refresh();
            if (operation.target_account_id && flow.status?.account_id === operation.target_account_id) await flow.restore(operation.target_account_id);
        } catch (err) { error = err; render(); }
    }
    function invitationOnboarding() {
        const nodes = [];
        if (inviteChoice) {
            const section = el("section", { class: "accounts-editor", "aria-label": t("accounts.invite.title") }, [el("h2", { text: t("accounts.invite.title") }), el("p", { text: t("accounts.invite.limits") })]);
            if (invitePreview) {
                section.append(el("p", { text: t("accounts.invite.source_preview", { account: accounts.find(a => a.id === invitePreview.source_account_id)?.label || invitePreview.source_account_id }) }));
                const field = c.textField.create({ id: "accounts-invite-link", labelKey: "accounts.referrals.link", value: invitePreview.link });
                const input = field.querySelector("input"); input.readOnly = true; input.dir = "ltr";
                section.append(field, el("time", { datetime: invitePreview.link_read_at, text: t("accounts.referrals.read_at", { date: i18n.formatDate(invitePreview.link_read_at), time: i18n.formatTime(invitePreview.link_read_at) }) }));
                if (invitePreview.unreadable) section.append(c.banner.create({ tone: "warning", messageKey: "accounts.invite.stale_link" }));
                section.append(button("accounts-invite-open", { labelKey: "accounts.invite.open", variant: "primary", disabled: invitePending, onClick: startInvitation }), button("accounts-invite-discard-preview", { labelKey: "accounts.invite.discard", variant: "ghost", disabled: invitePending, onClick: () => discardInvitation(invitePreview) }));
            } else {
                section.append(c.select.create({ id: "accounts-invite-source", labelKey: "accounts.invite.source", value: inviteSource, options: [{ value: "", label: t("accounts.invite.choose_source") }, ...accounts.map(account => ({ value: account.id, label: account.label }))], onChange: value => { inviteSource = value; render("accounts-invite-source"); } }));
                section.append(button("accounts-invite-preview", { labelKey: "accounts.invite.preview", disabled: invitePending || !inviteSource, onClick: () => previewInvitation() }), button("accounts-invite-normal", { labelKey: "accounts.invite.normal", variant: "ghost", disabled: invitePending, onClick: () => { inviteChoice = false; addAccount(false, true); } }), button("accounts-invite-cancel-choice", { labelKey: "common.action.cancel", variant: "ghost", onClick: () => { inviteChoice = false; render(); } }));
            }
            nodes.push(section);
        }
        for (const operation of inviteOperations.filter(op => op.operation_id !== invitePreview?.operation_id && (op.can_resume || op.can_discard || op.phase === "awaiting_confirmation" || op.recovery_only))) {
            const pending = operation.phase === "awaiting_confirmation";
            const actions = [];
            if (pending) actions.push(button(`accounts-invite-confirm-${operation.operation_id}`, { labelKey: "accounts.enrollment.begin", disabled: invitePending || confirming, onClick: async () => {
                await flow.restore(operation.target_account_id);
                if (flow.status?.account_id === operation.target_account_id && flow.status?.state === "awaiting_confirmation") await enrollment(flow.status);
            } }));
            else if (operation.can_resume && !operation.recovery_only) actions.push(button(`accounts-invite-resume-${operation.operation_id}`, { labelKey: "accounts.invite.resume", disabled: invitePending, onClick: () => previewInvitation(operation) }));
            if (operation.can_discard) actions.push(button(`accounts-invite-discard-${operation.operation_id}`, { labelKey: "accounts.invite.discard", variant: "ghost", disabled: invitePending, onClick: () => discardInvitation(operation) }));
            nodes.push(el("section", { class: "accounts-editor", "data-invite-operation": operation.operation_id }, [el("p", { text: t(pending ? "accounts.signin.awaiting_confirmation" : operation.recovery_only ? "accounts.invite.recovery" : "accounts.invite.interrupted") }), el("p", { text: t("accounts.invite.limits") }), ...actions]));
        }
        return nodes;
    }
    async function remove(account) {
        if (busyId || confirming) return;
        confirming = true;
        const choices = [{ id: "cancel", labelKey: "common.action.cancel", variant: "ghost" }];
        if (!account.is_root) choices.push({ id: "keep", labelKey: "accounts.remove.keep", variant: "danger" });
        choices.push({ id: "delete", labelKey: "accounts.remove.delete", variant: "danger" });
        let choice;
        try { choice = await c.dialog.open({ titleKey: "accounts.remove.title", bodyKey: account.is_root ? "accounts.remove.root_body" : "accounts.remove.body", params: { account: account.label }, actions: choices }); }
        finally { confirming = false; }
        if (!alive || !["keep", "delete"].includes(choice)) return;
        busyId = account.id;
        error = null;
        warnings = [];
        render();
        try {
            const data = await bridge.call("remove_account", { account_id: account.id, delete_data: choice === "delete" });
            if (!alive) return;
            sync(data.accounts);
            warnings = data.warnings;
            editId = null;
            if (!accounts.length) {
                // Only a redirect needs a toast to preserve the warning.
                for (const warning of warnings) c.toast.show({ tone: "warning", messageKey: warning.message_key, params: warning.params, timeoutMs: 0 });
                warnings = [];
                router.navigate("#/setup/1");
            }
            else if (!warnings.length) c.toast.show({ tone: "success", messageKey: "accounts.remove.success" });
        } catch (err) { if (alive) error = err; }
        finally { busyId = null; render(); }
    }
    function signinPanel() {
        const status = flow.status;
        if (status?.state === "awaiting_confirmation" && confirming) return null;
        if (!status) return flow.error ? c.banner.create({ tone: "error", messageKey: flow.error.messageKey, params: flow.error.params }) : null;
        const active = activeStates.has(status.state);
        const children = [];
        if (active) children.push(c.spinner.create({ labelKey: statusKeys[status.state] }));
        children.push(c.banner.create({ tone: status.error_code === "terms_of_use" ? "warning" : status.state === "signed_in" ? "success" : ["busy", "failed", "timed_out"].includes(status.state) ? "warning" : "info", messageKey: flow.cancelling ? "accounts.signin.cancelling" : status.message_key || statusKeys[status.state], params: { account: accounts.find((a) => a.id === status.account_id)?.label } }));
        if (flow.error) children.push(c.banner.create({ tone: "error", messageKey: flow.error.messageKey, params: flow.error.params }));
        if (active) {
            children.push(button("accounts-cancel-signin", { labelKey: "common.action.cancel", onClick: flow.cancel, disabled: flow.cancelling }));
            if (status.state === "invite_waiting") children.push(el("p", { text: t("accounts.invite.limits") }), button("accounts-invite-continue", { labelKey: "accounts.invite.continue", variant: "primary", disabled: flow.pending || flow.cancelling || status.invitation?.phase !== "invite_open", onClick: flow.continueInvite }));
        }
        else if (status.state === "awaiting_confirmation") children.push(button("accounts-confirm-enrollment", { labelKey: "accounts.enrollment.begin", onClick: () => enrollment(status), disabled: confirming || flow.cancelling }), button("accounts-cancel-enrollment", { labelKey: "accounts.enrollment.cancel", variant: "ghost", onClick: flow.cancel, disabled: confirming || flow.cancelling }));
        else if (status.state !== "signed_in" || status.error_code === "terms_of_use") children.push(button("accounts-retry-signin", { labelKey: "accounts.signin.retry", onClick: () => accounts.some((a) => a.id === status.account_id) ? signin(accounts.find((a) => a.id === status.account_id)) : addAccount(), disabled: flow.pending }));
        return el("section", { class: "accounts-signin", "aria-label": t("accounts.signin.title"), "aria-live": "polite", "data-signin-state": status.state }, children);
    }
    function accountEntry(account) {
        const id = keyId(account);
        const locked = Boolean(busyId) || confirming || flow.pending || activeStates.has(flow.status?.state) || flow.status?.state === "awaiting_confirmation";
        const actions = [
            button(`accounts-signin-${id}`, { labelKey: "accounts.action.signin_again", disabled: locked, onClick: () => signin(account) }),
            button(`accounts-rename-${id}`, { labelKey: "accounts.action.rename", variant: "ghost", disabled: locked, onClick: () => { editId = account.id; label = account.label; error = null; render("accounts-name"); } }),
        ];
        if (!account.is_root) actions.push(button(`accounts-pause-${id}`, { labelKey: account.paused ? "accounts.action.resume" : "accounts.action.pause", variant: "ghost", disabled: locked, onClick: () => action(account, "set_account_paused", { account_id: account.id, paused: !account.paused }, account.paused ? "accounts.resumed" : "accounts.paused") }));
        const removeAction = button(`accounts-remove-${id}`, { labelKey: "common.action.remove", variant: "ghost", disabled: locked, onClick: () => remove(account) });
        removeAction.classList.add("button-danger");
        actions.push(removeAction);
        const more = disclosure("accounts.action.more", actions.slice(1), account.id, openActions.has(account.id) || editId === account.id);
        more.id = `accounts-more-${id}`;
        more.querySelector("summary").id = `accounts-more-toggle-${id}`;
        const row = c.accountRow.create({ account, actions: [actions[0], more] });
        if (!Number.isFinite(account.balance)) row.querySelector(".account-row-balance")?.remove();
        const entry = el("div", { class: "accounts-entry" }, row);
        if (Object.hasOwn(account, "identity_status")) {
            const identity = ["confirmed", "unconfirmed"].includes(account.identity_status) ? account.identity_status : "unknown";
            const check = ["match", "mismatch"].includes(account.identity_last_status) ? account.identity_last_status : "unknown";
            const identityInfo = el("div", { class: "accounts-identity", "data-identity": identity, "data-identity-check": check }, [el("span", { text: t(`accounts.identity.${identity}`) }), el("span", { text: t("accounts.identity.last_check", { result: t(`accounts.identity.${check}`) }) })]);
            if (account.identity_checked_at) identityInfo.append(el("time", { datetime: account.identity_checked_at, text: t("accounts.identity.checked_at", { date: i18n.formatDate(account.identity_checked_at), time: i18n.formatTime(account.identity_checked_at) }) }));
            if (identity !== "confirmed") identityInfo.append(button(`accounts-enroll-${id}`, { labelKey: "accounts.enrollment.begin", variant: "ghost", disabled: locked, onClick: () => beginEnrollment(account) }));
            entry.append(identityInfo);
        }
        if (account.is_root) entry.append(el("p", { class: "accounts-help", text: t("accounts.root_pause") }));
        if (editId === account.id) {
            const save = () => action(account, "rename_account", { account_id: account.id, label: label.trim() }, "accounts.rename.saved");
            entry.append(el("div", { class: "accounts-editor" }, [c.textField.create({ id: "accounts-name", labelKey: "accounts.rename.label", descriptionKey: "accounts.rename.description", value: label, maxLength: 40, autocomplete: "off", disabled: Boolean(busyId), error: error ? { messageKey: error.messageKey, params: error.params } : null, onInput: (value) => { label = value; }, onCommit: (value) => { label = value; } }),
                el("div", { class: "control-actions" }, [button("accounts-save-name", { labelKey: "common.action.save", variant: "primary", busy: busyId === account.id, onClick: save }), button("accounts-cancel-name", { labelKey: "common.action.cancel", variant: "ghost", disabled: Boolean(busyId), onClick: () => { editId = null; error = null; render(`accounts-rename-${id}`); } })]),
            ]));
        }
        return entry;
    }
    function render(focusId) {
        if (!alive) return;
        const focused = root.contains(document.activeElement) ? document.activeElement.id : null;
        c.dispose(root);
        root.replaceChildren(el("header", { class: "screen-header" }, el("h1", { text: t("accounts.title") })));
        if (error) root.append(c.banner.create({ tone: "error", messageKey: error.messageKey, params: error.params }));
        for (const warning of warnings) root.append(c.banner.create({ tone: "warning", messageKey: warning.message_key, params: warning.params }));
        const panel = signinPanel();
        if (panel) root.append(panel);
        root.append(...invitationOnboarding());
        if (!loaded && error) root.append(c.emptyState.create({ titleKey: "accounts.load.title", action: { labelKey: "common.action.retry", onClick: () => { error = null; refresh(); } } }));
        else if (!accounts.length) root.append(c.emptyState.create({ icon: "accounts", titleKey: "accounts.empty.title", bodyKey: "accounts.empty.body", action: { labelKey: "accounts.empty.action", variant: "primary", onClick: () => router.navigate("#/setup/1") } }));
        else root.append(el("div", { class: "accounts-list" }, [...accounts.map(accountEntry), button("accounts-add-another", { labelKey: "accounts.enrollment.add", disabled: Boolean(busyId) || confirming || flow.pending || activeStates.has(flow.status?.state), onClick: () => addAccount() }), ...invited.listItems()]));
        if (accounts.length) root.append(invited.referralSection());
        const nextFocus = document.getElementById(focusId || focused);
        if (nextFocus && !nextFocus.disabled) nextFocus.focus({ preventScroll: true });
        else if (focused) (root.querySelector("#accounts-cancel-signin:not(:disabled), #accounts-retry-signin:not(:disabled), .account-row button:not(:disabled)") || view).focus({ preventScroll: true });
    }
    await refresh();
    flow.seed();
    const activeId = store.get("overview")?.signin_active?.account_id;
    if (activeId && !flow.status) await flow.restore(activeId);
    return { update() { render(); }, unmount() { alive = false; flow.dispose(); invited.dispose(); c.dispose(root); } };
}
