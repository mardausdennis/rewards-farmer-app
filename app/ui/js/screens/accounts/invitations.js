import { el } from "../../dom.js";

export const referralUnreadableCodes = ["not_read", "signed_out", "challenge", "page_unknown", "link_missing", "link_ambiguous", "link_invalid", "read_failed", "storage_failed", "storage_unreadable"];

export function invitations(ctx, changed) {
    const { bridge, i18n, components: c } = ctx;
    const t = i18n.t;
    let alive = true, generation = 0, loading = true, pending = false;
    let friends = [], friendsError = null, referral = null, referralError = null, edit = null, saveError = null;
    const button = (id, props) => { const node = c.button.create(props); node.id = id; return node; };
    const editButtonId = (id) => `accounts-friend-edit-${id}`;

    async function load() {
        const token = ++generation;
        loading = true;
        friendsError = referralError = null;
        changed();
        await Promise.all([
            bridge.call("get_invited_friends").then((data) => { if (alive && token === generation) friends = data.invited_friends; })
                .catch((error) => { if (alive && token === generation) friendsError = error; }),
            bridge.call("get_referral_info").then((data) => { if (alive && token === generation) referral = data; })
                .catch((error) => { if (alive && token === generation) { referral = null; referralError = error; } }),
        ]);
        if (alive && token === generation) { loading = false; changed(); }
    }

    function begin(friend = null) {
        edit = { id: friend?.id, name: friend?.name || "", email: friend?.email || "" };
        saveError = null;
        changed("accounts-friend-name");
    }

    async function save(form) {
        if (pending || !edit || !form.reportValidity()) return;
        const request = { name: edit.name.trim(), email: edit.email.trim(), ...(edit.id ? { id: edit.id } : {}) };
        pending = true;
        saveError = null;
        changed();
        let focus = "accounts-friend-name";
        try {
            const { friend } = await bridge.call("save_invited_friend", request);
            if (!alive) return;
            friends = request.id ? friends.map((old) => old.id === friend.id ? friend : old) : [...friends, friend];
            edit = null;
            focus = editButtonId(friend.id);
            c.toast.show({ tone: "success", messageKey: "accounts.invited.saved" });
        } catch (error) { if (alive) saveError = error; }
        finally { pending = false; if (alive) changed(focus); }
    }

    async function remove(friend) {
        if (pending) return;
        pending = true;
        let choice;
        try {
            choice = await c.dialog.open({ titleKey: "accounts.invited.remove_title", bodyKey: "accounts.invited.remove_body", params: { name: friend.name },
                actions: [{ id: "cancel", labelKey: "common.action.cancel", variant: "ghost" }, { id: "remove", labelKey: "accounts.invited.remove", variant: "danger" }] });
        } finally { pending = false; }
        if (!alive || choice !== "remove") return;
        pending = true;
        saveError = null;
        changed();
        try {
            const data = await bridge.call("remove_invited_friend", { id: friend.id });
            if (!alive) return;
            friends = data.invited_friends;
            if (edit?.id === friend.id) edit = null;
            c.toast.show({ tone: "success", messageKey: "accounts.invited.removed" });
        } catch (error) { if (alive) saveError = error; }
        finally { pending = false; if (alive) changed("accounts-friend-add"); }
    }

    function editor() {
        const form = el("form", { class: "accounts-editor accounts-friend-editor", "aria-label": t(edit.id ? "accounts.invited.edit" : "accounts.invited.add") });
        const name = c.textField.create({ id: "accounts-friend-name", labelKey: "accounts.invited.name", value: edit.name, maxLength: 40, autocomplete: "off", disabled: pending, onInput: (value) => { edit.name = value; } });
        const email = c.textField.create({ id: "accounts-friend-email", labelKey: "accounts.invited.email", value: edit.email, maxLength: 254, autocomplete: "off", disabled: pending, onInput: (value) => { edit.email = value; } });
        const nameInput = name.querySelector("input");
        nameInput.required = true;
        nameInput.pattern = ".*\\S.*";
        const address = email.querySelector("input");
        address.type = "email";
        address.required = true;
        address.dir = "ltr";
        address.spellcheck = false;
        form.append(name, email, el("div", { class: "control-actions" }, [
            button("accounts-friend-save", { labelKey: "accounts.invited.save", variant: "primary", busy: pending, onClick: () => save(form) }),
            button("accounts-friend-cancel", { labelKey: "common.action.cancel", variant: "ghost", disabled: pending, onClick: () => { const id = edit.id; edit = null; saveError = null; changed(id ? editButtonId(id) : "accounts-friend-add"); } }),
        ]));
        form.addEventListener("submit", (event) => { event.preventDefault(); save(form); });
        return form;
    }

    function friendEntry(friend) {
        const automatic = c.switchControl.create({ id: `accounts-friend-auto-${friend.id}`, labelKey: "accounts.invited.auto_farm", descriptionKey: "accounts.invited.auto_farm_disabled", checked: false, disabled: true });
        automatic.classList.add("accounts-friend-switch");
        const control = automatic.querySelector("[role=switch]");
        control.setAttribute("aria-disabled", "true");
        control.tabIndex = -1;
        const removeButton = button(`accounts-friend-remove-${friend.id}`, { labelKey: "accounts.invited.remove", variant: "ghost", disabled: pending, onClick: () => remove(friend) });
        removeButton.classList.add("button-danger");
        const row = c.accountRow.create({ account: { id: friend.id, label: friend.name, name: friend.name, email: friend.email, balance: null }, actions: [automatic,
            button(editButtonId(friend.id), { labelKey: "accounts.invited.edit", variant: "ghost", disabled: pending, onClick: () => begin(friend) }), removeButton] });
        row.removeAttribute("data-account");
        row.dataset.friend = friend.id;
        row.querySelector(".account-row-balance").remove();
        const status = row.querySelector(".account-row-status");
        status.classList.add("accounts-invited-chip");
        status.textContent = t("accounts.invited.status");
        const entry = el("div", { class: "accounts-entry accounts-friend", "data-invited": friend.id }, row);
        if (edit?.id === friend.id) entry.append(editor());
        return entry;
    }

    function listItems() {
        const items = [el("div", { class: "accounts-invited-heading" }, [el("h2", { id: "accounts-invited-title", text: t("accounts.invited.title") }),
            button("accounts-friend-add", { labelKey: "accounts.invited.add", variant: "ghost", disabled: loading || pending, onClick: () => begin() })])];
        if (loading) items.push(c.spinner.create({ labelKey: "common.action.working" }));
        else if (friendsError) items.push(c.banner.create({ tone: "error", messageKey: friendsError.messageKey, params: friendsError.params, actions: [{ labelKey: "common.action.retry", onClick: load }] }));
        else if (!friends.length) items.push(el("p", { class: "accounts-invited-empty", text: t("accounts.invited.empty") }));
        if (saveError) items.push(c.banner.create({ tone: "error", messageKey: saveError.messageKey, params: saveError.params }));
        items.push(...friends.map(friendEntry));
        if (edit && !edit.id) items.push(editor());
        return items;
    }

    function referralSection() {
        const section = el("section", { class: "accounts-referrals", "aria-labelledby": "accounts-referrals-title" }, el("h2", { id: "accounts-referrals-title", text: t("accounts.referrals.title") }));
        if (loading) { section.append(c.spinner.create({ labelKey: "common.action.working" })); return section; }
        if (referralError) { section.append(c.banner.create({ tone: "error", messageKey: referralError.messageKey, params: referralError.params, actions: [{ labelKey: "common.action.retry", onClick: load }] })); return section; }
        if (!referral) return section;
        if (typeof referral.link === "string" && referral.link) {
            const field = c.textField.create({ id: "accounts-referral-link", labelKey: "accounts.referrals.link", value: referral.link });
            const input = field.querySelector("input");
            input.readOnly = true;
            input.dir = "ltr";
            input.spellcheck = false;
            const feedback = el("p", { class: "accounts-referral-feedback", role: "status", "aria-live": "polite" });
            const copy = async () => {
                try {
                    if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
                    await navigator.clipboard.writeText(input.value);
                    if (alive && feedback.isConnected) feedback.textContent = t("accounts.referrals.copied");
                } catch {
                    if (alive && input.isConnected) { input.focus(); input.select(); feedback.textContent = t("accounts.referrals.select"); }
                }
            };
            section.append(el("div", { class: "accounts-referral-copy" }, [field, button("accounts-referral-copy", { labelKey: "accounts.referrals.copy", onClick: copy })]), feedback);
        } else section.append(el("p", { class: "accounts-referral-unavailable", text: t("accounts.referrals.link_unknown") }));
        const figures = el("dl", { class: "accounts-referral-figures" });
        for (const [key, count] of [["referrals", referral.referrals], ["friends", Array.isArray(referral.friends) ? referral.friends.length : null]]) {
            figures.append(el("div", {}, [el("dt", { text: t(`accounts.referrals.${key}`) }), el("dd", { "data-referral-figure": key, text: Number.isInteger(count) && count >= 0 ? i18n.formatNumber(count) : t("accounts.referrals.unknown") })]));
        }
        section.append(figures);
        if (referral.read_at) section.append(el("p", { class: "accounts-referral-read" }, el("time", { datetime: referral.read_at, text: t("accounts.referrals.read_at", { date: i18n.formatDate(referral.read_at), time: i18n.formatTime(referral.read_at) }) })));
        if (referral.unreadable !== null) {
            const code = referralUnreadableCodes.includes(referral.unreadable) ? referral.unreadable : "storage_unreadable";
            section.append(el("p", { class: "accounts-referral-status", role: "status", "data-referral-unreadable": code, text: t(`accounts.referrals.unreadable.${code}`) }));
        }
        return section;
    }

    return { load, listItems, referralSection, dispose() { alive = false; generation++; } };
}
