export const activeStates = new Set(["starting", "waiting", "verifying"]);
export const statusKeys = {
    starting: "accounts.signin.starting", waiting: "accounts.signin.waiting",
    verifying: "accounts.signin.verifying", signed_in: "accounts.signin.signed_in",
    failed: "accounts.signin.failed", timed_out: "accounts.signin.timed_out",
    cancelled: "accounts.signin.cancelled", busy: "accounts.signin.busy",
    awaiting_confirmation: "accounts.signin.awaiting_confirmation",
};

// Both owned screens follow the same backend state, without polling or opening a URL.
export function signinFlow(ctx, { accepts = () => true, changed, connected, awaiting }) {
    let alive = true;
    let target = null;
    let revision = 0;
    const flow = { status: null, error: null, pending: false, cancelling: false };
    const notify = () => { if (alive) changed(); };
    function receive(status, own = false) {
        if (!alive || !status || !Object.hasOwn(statusKeys, status.state) || (!own && !accepts(status.account_id))) return;
        if (target && target !== status.account_id) return;
        const previous = flow.status;
        revision++;
        flow.status = status;
        target = status.account_id;
        flow.error = null;
        if (!activeStates.has(status.state)) flow.cancelling = false;
        notify();
        if (status.state === "signed_in" && (previous?.state !== "signed_in" || previous?.account_id !== status.account_id)) connected?.(status);
        if (status.state === "awaiting_confirmation" && (previous?.state !== status.state || previous?.account_id !== status.account_id)) awaiting?.(status);
    }
    const off = ctx.store.on("signin", (status) => receive(status));
    flow.restore = async (accountId) => {
        target = accountId;
        const token = revision;
        try {
            const status = await ctx.bridge.call("get_signin_status", { account_id: accountId });
            if (token === revision) receive(status, true);
        } catch (error) { if (alive && token === revision) { flow.error = error; notify(); } }
    };
    flow.start = async (method, request = {}) => {
        if (flow.pending || activeStates.has(flow.status?.state)) return;
        target = request.account_id || null;
        flow.pending = true;
        flow.cancelling = false;
        flow.error = null;
        const token = revision;
        notify();
        try {
            const result = await ctx.bridge.call(method, request);
            if (alive && token === revision) receive(result.signin, true);
            return result;
        } catch (error) { if (alive) { flow.error = error; notify(); } throw error; }
        finally { flow.pending = false; notify(); }
    };
    flow.cancel = async () => {
        if ((!activeStates.has(flow.status?.state) && flow.status?.state !== "awaiting_confirmation") || flow.cancelling) return;
        flow.cancelling = true;
        flow.error = null;
        notify();
        try {
            await ctx.bridge.call("cancel_signin", { account_id: flow.status.account_id });
            if (flow.status?.state === "awaiting_confirmation") receive({ ...flow.status, state: "cancelled", message_key: statusKeys.cancelled }, true);
        }
        catch (error) { if (alive) { flow.cancelling = false; flow.error = error; notify(); } }
    };
    flow.clearError = () => { flow.error = null; notify(); };
    flow.confirmed = () => {
        if (!flow.status) return;
        revision++;
        flow.status = { ...flow.status, state: "signed_in", message_key: statusKeys.signed_in };
        flow.error = null;
        flow.cancelling = false;
        ctx.store.set("signin", flow.status);
        notify();
    };
    flow.seed = () => receive(ctx.store.get("signin"));
    flow.dispose = () => { alive = false; off(); };
    return flow;
}
