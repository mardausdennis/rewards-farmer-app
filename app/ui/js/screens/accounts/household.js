import { el } from "../../dom.js";

const acknowledgedMethods = new Set(["add_account", "begin_enrollment", "preview_invite_account", "start_invite_account", "confirm_enrollment"]);

export function householdFlow(ctx, accounts) {
    const { bridge, i18n, components: c } = ctx;
    let acknowledged = false, pending = null, revision = 0;
    const reset = () => { acknowledged = false; revision++; };
    const cancelled = () => Object.assign(new Error("Household decision cancelled"), { householdCancelled: true });
    async function ensure(error = null) {
        if (acknowledged) return true;
        const count = new Set(accounts().map(account => account.id.toLowerCase()).filter(id => id !== "@root")).size;
        if (!error && count < 6) return true;
        if (pending) return pending;
        const token = revision;
        const checkbox = el("input", { id: "accounts-household-confirm", type: "checkbox" });
        const source = el("p", { class: "accounts-household-source" }, [el("span", { text: i18n.t("accounts.household.source") }), el("span", { dir: "ltr", text: "https://www.microsoft.com/servicesagreement" })]);
        const content = el("div", { class: "accounts-household" }, [el("blockquote", { text: i18n.t("accounts.household.terms") }), source, el("label", { class: "accounts-household-check", for: checkbox.id }, [checkbox, el("span", { text: i18n.t("accounts.household.confirm") })])]);
        checkbox.addEventListener("change", () => {
            const button = document.querySelector('#dialog [data-dialog-action="add"]');
            if (button) button.disabled = !checkbox.checked;
        });
        pending = c.dialog.open({ titleKey: "accounts.household.title", bodyKey: "errors.app.accounts_household_limit", params: { count: error?.params?.count ?? count, limit: 6 }, content, defaultAction: "cancel", actions: [
            { id: "cancel", labelKey: "common.action.cancel", variant: "ghost" }, { id: "add", labelKey: "accounts.household.add", variant: "primary", disabled: true },
        ] }).then(choice => {
            const allowed = choice === "add" && checkbox.checked && token === revision;
            if (allowed) acknowledged = true;
            else reset();
            return allowed;
        }).finally(() => { pending = null; });
        return pending;
    }
    async function call(method, request = {}) {
        const token = revision;
        const packet = () => acknowledged && acknowledgedMethods.has(method) ? { ...request, household_ack: true } : request;
        try { return await bridge.call(method, packet()); }
        catch (error) {
            if (token !== revision) throw cancelled();
            if (error.code !== "accounts.household_limit" || !acknowledgedMethods.has(method) || acknowledged) throw error;
            if (!await ensure(error)) throw cancelled();
            return bridge.call(method, packet());
        }
    }
    return { ensure, call, reset };
}
