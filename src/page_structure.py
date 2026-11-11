"""Value-free readiness evidence, at most two attempts per account/run."""
from weakref import WeakKeyDictionary

import run_record

_captured = WeakKeyDictionary()
_CODES = ("earn_tab", "breakdown", "cards", "quests", "streaks", "headers", "profile_buttons", "header_figures", "signin", "consent", "error")
_COUNTS = ("headers", "buttons", "main", "sections", "iframes", "headings", "shadow_roots")
SNAPSHOT = r"""
const q=s=>[...document.querySelectorAll(s)];
const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0'&&!e.closest('[hidden],[aria-hidden="true"]');};
const yes=s=>q(s).filter(visible);
const hydrated=(id,children)=>yes(id).filter(e=>(e.innerText||'').trim()&&[...e.querySelectorAll(children)].some(visible));
const matched={earn_tab:yes('[id$="-tab-/earn"]').length,
 breakdown:yes('button').filter(e=>(e.innerText||'').toLowerCase().includes('points breakdown')).length,
 cards:hydrated('#moreactivities','a').length,quests:hydrated('#quests','a').length,
 streaks:hydrated('#streaks','button,[role="progressbar"]').length,headers:yes('header').length,
 profile_buttons:yes('header button').filter(e=>(e.getAttribute('aria-label')||'').trim().toLowerCase()==='view profile').length,
 header_figures:yes('header button[aria-label]').reduce((n,button)=>n+[...button.querySelectorAll(':scope > span > div > div')].filter(row=>row.querySelector(':scope > img')).flatMap(row=>[...row.querySelectorAll(':scope > p')]).filter(visible).length,0),
 signin:yes('input[type="password"],#i0116,#idSIButton9').length,
 consent:yes('#bnp_container,[id*="consent" i]').length,error:yes('#error,#errorPage,#main-frame-error,iframe[src*="captcha" i]').length};
const host=location.hostname==='rewards.bing.com',earn=location.pathname.replace(/\/$/,'')==='/earn';
return {complete:document.readyState==='complete',rewards_host:host,earn_path:earn,
 rendered:host&&earn&&document.readyState==='complete'&&matched.earn_tab>0&&!!(matched.cards||matched.quests||matched.streaks)&&!matched.signin&&!matched.consent&&!matched.error,
 counts:{headers:q('header').length,buttons:q('button').length,main:q('main').length,sections:q('section').length,iframes:q('iframe').length,headings:q('h1,h2,h3,h4,h5,h6').length,shadow_roots:q('*').filter(e=>e.shadowRoot).length},matched};
"""


def _count(value):
	return min(value, 10000) if type(value) is int and value >= 0 else 0


def snapshot(driver):
	packet = driver.execute_script(SNAPSHOT)
	return _sanitize(packet)


def _sanitize(packet):
	if not isinstance(packet, dict):
		raise ValueError("structure unavailable")
	# Allowlist again before persistence; no text, numeric page values or URL
	# can cross this boundary even if a driver returns extra fields.
	counts = packet.get("counts") if isinstance(packet.get("counts"), dict) else {}
	matched = packet.get("matched") if isinstance(packet.get("matched"), dict) else {}
	return {**{key: packet.get(key) is True for key in ("complete", "rewards_host", "earn_path", "rendered")},
		"counts": {key: _count(counts.get(key)) for key in _COUNTS},
		"matched": {key: _count(matched.get(key)) for key in _CODES}}


def needs_reload(packet):
	matched = packet["matched"]
	return (all(packet[key] for key in ("complete", "rewards_host", "earn_path"))
		and matched["earn_tab"] > 0 and not any(matched[key] for key in ("signin", "consent", "error"))
		and any(matched[key] == 0 for key in ("cards", "quests", "breakdown", "header_figures")))


def missing_sections(packet):
	return ",".join(section for code, section in (("breakdown", "searches"), ("cards", "cards"),
		("quests", "quests"), ("header_figures", "balance")) if packet["matched"][code] == 0)


def capture_once(driver, reason, *, attempt=1, reloaded=False, packet=None):
	if (type(attempt) is not int or attempt not in (1, 2) or not isinstance(reason, str) or not reason
		or any(code not in ("page_blank", "signed_out", "terms_of_use", "searches", "daily_set", "cards", "quests", "balance") for code in reason.split(","))):
		return
	try:
		recorder, account = run_record.current(), run_record._account.get()
		if recorder is None or account is None:
			return
		attempts = _captured.setdefault(recorder, {}).setdefault(account, set())
		if attempt in attempts:
			return
		attempts.add(attempt)
		fields = {"reason": reason, "attempt": attempt, "reloaded": reloaded is True}
		try:
			if reason in ("signed_out", "terms_of_use"):
				# Do not inspect the stale/private redirect document as an earn page.
				raise ValueError("earn document unavailable")
			if packet is None:
				from rewards_tasks import _balance_command_deadline
				import time
				with _balance_command_deadline(driver, time.monotonic() + 2, time.monotonic):
					packet = snapshot(driver)
			run_record.emit("reader_structure", **fields, status="observed", **_sanitize(packet))
		except Exception:
			run_record.emit("reader_structure", **fields, status="unavailable")
	except Exception:
		# Diagnostics must not change the actual read outcome.
		pass
