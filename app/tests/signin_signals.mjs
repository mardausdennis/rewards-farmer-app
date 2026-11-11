// Execute the production boolean-only auth probe without a browser or network.
import fs from "node:fs";
import vm from "node:vm";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
let effects = 0;
const refused = () => { effects++; throw Error("unexpected browser effect"); };
function element(raw) {
    return {
        innerText: raw.text || "", getAttribute: key => raw[key] || null,
        getBoundingClientRect: () => ({ width: raw.hidden ? 0 : 100, height: raw.hidden ? 0 : 20 }),
        closest: () => raw.hidden ? {} : null,
        click: refused,
    };
}
const bySelector = {
    "p.pii": (input.identity || []).map(element),
    'header button, header [role="button"]': (input.profile || []).map(element),
    'a[href], form[action], button, [role="button"]': (input.controls || []).map(element),
};
const document = { querySelectorAll: selector => {
    if (!Object.hasOwn(bySelector, selector)) throw Error("unexpected selector");
    return bySelector[selector];
} };
const window = { location: { href: input.url || "https://rewards.bing.com/", assign: refused, replace: refused } };
const result = vm.runInNewContext(`(function(){${input.script}})()`, {
    document, window, URL, getComputedStyle: () => ({display:"block", visibility:"visible", opacity:"1"}), fetch: refused,
}, { timeout: 2000 });
process.stdout.write(JSON.stringify({ result, effects }));
