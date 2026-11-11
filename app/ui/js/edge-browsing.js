import { el } from "./dom.js";

const outcomes = new Set(["complete", "incomplete", "not_available", "stopped", "error"]);

export function edgeBrowsingLine(i18n, account) {
    const result = account.edge_browsing;
    if (!result || !outcomes.has(result.outcome)) return null;
    const number = value => Number.isInteger(value) && value >= 0 ? i18n.formatNumber(value) : i18n.t("common.value.unknown");
    return el("p", { class: "edge-browsing-result subtle", "data-edge-account": account.account_id, "data-edge-outcome": result.outcome, text: i18n.t("common.edge_browsing.result", {
        before: number(result.minutes_before), after: number(result.minutes_after), target: number(result.minutes_target), outcome: i18n.t(`common.edge_browsing.outcome.${result.outcome}`),
    }) });
}
