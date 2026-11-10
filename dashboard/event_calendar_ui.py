"""Compact upcoming market catalysts UI; rendering is SQLite-only."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import streamlit as st

from dashboard.data_access import (
    add_earnings_watch_symbol, list_earnings_watchlist,
    list_holdings, list_market_event_revisions, list_market_event_source_status, list_market_events,
    list_research_workspaces, remove_earnings_watch_symbol,
    save_manual_public_event, save_quick_earnings_events,
)
from dashboard.event_calendar import refresh_official_calendars
from dashboard.event_calendar import filter_upcoming_events
from dashboard.earnings_parser import parse_quick_earnings
from dashboard.event_import import approve_event_import, list_import_batches, rollback_event_import
from dashboard.compact_event_import import (
    apply_compact_preview_edits, build_compact_discovery_prompt, parse_compact_events,
)
from dashboard.theme import (
    CALENDAR_BODY_FONT_SIZE, CALENDAR_CARD_MIN_HEIGHT, CALENDAR_CARD_PADDING,
    CALENDAR_CARD_WIDTH, CALENDAR_DATE_FONT_SIZE, CALENDAR_META_FONT_SIZE,
    CALENDAR_SECONDARY_FONT_SIZE, CALENDAR_TITLE_FONT_SIZE,
)

ET = "America/New_York"
CATEGORY_COLORS = {"Macro": "#7aa2f7", "Fed": "#c4a7e7", "Earnings": "#7fc8a9", "Political/Other": "#e6b566"}


def _source_link(url: str) -> str:
    url = str(url or "")
    if not (url.startswith("https://") or url.startswith("http://")):
        return ""
    return url


def _format_countdown(start: datetime, now: datetime) -> str:
    seconds = max(0, int((start - now).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    if days == 0:
        return f"in {hours}h" if hours else "today"
    return f"in {days}d" if days == 1 else f"in {days} days"


def _display_event_datetime(event: dict, zone: ZoneInfo) -> tuple[str, str, datetime]:
    utc = datetime.fromisoformat(event["start_utc"].replace("Z", "+00:00"))
    event_day = date.fromisoformat(event.get("event_date") or event["start_utc"][:10])
    session_label = {"BMO": "Before Market Open", "AMC": "After Market Close", "TBD": "Time TBD"}.get(event.get("earnings_session"))
    if event.get("category") == "Earnings" and session_label and not event.get("exact_announcement_time_utc"):
        return event_day.strftime("%a, %b %-d"), session_label, utc
    if event["all_day"]:
        display_date = event_day.strftime("%a, %b %-d")
        return display_date, "Time not specified", utc
    local = utc.astimezone(zone)
    return local.strftime("%a, %b %-d"), local.strftime("%-I:%M %p %Z"), utc


def _why_it_matters(event: dict) -> tuple[str, str]:
    category = event["category"]
    name = event["name"].lower()
    if category == "Fed":
        return ("Policy communication can reset rate expectations and influence broad equity and volatility pricing; the actual reaction is unknown in advance.",
                "Monitor SPY and QQQ relative performance, RSP participation proxy, VIX, and any rate-sensitive exposures.")
    if category == "Macro":
        if "consumer price" in name or "price index" in name or "personal income and outlays" in name:
            return ("Inflation and spending releases can affect rate expectations and earnings assumptions; no direction is implied.",
                    "Monitor SPY, sector leadership, VIX, and the stored release date/time. Rates/yields are not currently integrated.")
        if "employment" in name or "job openings" in name or "payroll" in name:
            return ("Labor releases inform growth and wage conditions and can affect rate expectations; this is context, not a forecast.",
                    "Monitor SPY/RSP relative performance, sector dispersion, and VIX.")
        return ("This scheduled economic release may update growth or inflation context. Market reaction is not predictable from the calendar alone.",
                "Monitor cached market leadership, sector rotation, and VIX around the release.")
    if category == "Earnings":
        return ("Company results or guidance may update expectations for the named issuer; the calendar does not indicate the direction of any move.",
                "Monitor the issuer's official release, saved strategy assumptions, and subsequent cached quote/history when available.")
    return ("This date may be relevant to policy or political uncertainty, but the timeline does not infer a market reaction.",
            "Monitor verified developments and broad market indicators; no political catalyst interpretation is inferred from price data.")


def _stale_after_days(provider: str) -> int:
    if provider == "bls_ical":
        return 8
    if provider in ("federal_reserve_fomc", "federal_reserve_speeches"):
        return 60
    return 30


def render_upcoming_market_catalysts(*, show_refresh_button: bool = True) -> list[dict]:
    """Render the timeline and return verified upcoming records for an explicit AI action."""
    if show_refresh_button:
        title_col, refresh_col, manage_col = st.columns([6, 1, 1.35], vertical_alignment="center")
    else:
        title_col, manage_col = st.columns([7, 1.35], vertical_alignment="center")
        refresh_col = None
    title_col.markdown("#### Upcoming Market Catalysts")
    title_col.caption("All times ET")
    if show_refresh_button and refresh_col.button("↻ Refresh", key="refresh_market_events", help="Refresh official calendars", use_container_width=True):
        with st.spinner("Refreshing official event calendars…"):
            st.session_state["event_calendar_refresh_result"] = refresh_official_calendars()
        st.rerun()
    with manage_col.popover("⚙ Manage", use_container_width=True):
        _render_earnings_manager()

    refresh_result = st.session_state.pop("event_calendar_refresh_result", None)
    if refresh_result:
        totals = sum(sum(stats.values()) for stats in refresh_result.get("updated", {}).values())
        st.caption(f"Calendar refresh complete · {totals} event records inserted, changed, or verified.")
        if refresh_result.get("errors"):
            with st.expander("Calendar refresh details", expanded=False):
                for provider, message in refresh_result["errors"].items():
                    st.caption(f"**{provider}:** {message}")

    controls = st.columns([1, 2.2, 1.5, 2.1])
    horizon = controls[0].selectbox("Window", (30, 60), index=0, format_func=lambda n: f"Next {n} days", key="event_window_days")
    category = controls[1].segmented_control(
        "Category", ("All", "Macro", "Fed", "Earnings", "Political/Other"),
        default="All", key="event_category_filter", label_visibility="collapsed",
    )
    category = category or "All"
    verification = controls[2].selectbox("Status", ("All", "Verified source", "Estimated", "Tentative", "Unverified"), key="event_verification_filter")

    now = datetime.now(timezone.utc)
    zone = ZoneInfo(ET)
    all_events = list_market_events()
    symbols_available = sorted({symbol for event in all_events for symbol in event.get("related_symbols", [])})
    selected_symbols = controls[3].multiselect("Tickers", symbols_available, default=[], placeholder="All tickers", key="event_ticker_filter")
    scope = st.radio("Scope", ("All events", "Market-wide", "Company-specific"), horizontal=True, key="event_scope_filter")
    events = filter_upcoming_events(all_events, category=category, horizon_days=horizon, now_utc=now)
    if verification == "Verified source":
        events = [event for event in events if event.get("source_verification") == "Official source"]
    elif verification == "Unverified":
        events = [event for event in events if event.get("status") == "Unverified" or event.get("source_verification") != "Official source"]
    elif verification != "All":
        events = [event for event in events if event.get("status") == verification]
    if selected_symbols:
        events = [event for event in events if set(selected_symbols) & set(event.get("related_symbols", []))]
    if scope == "Market-wide":
        events = [event for event in events if not event.get("related_symbols")]
    elif scope == "Company-specific":
        events = [event for event in events if event.get("related_symbols")]
    holdings_symbols = {row["symbol"].upper() for row in list_holdings()}
    workspace_symbols = set(list_research_workspaces())
    source_statuses = list_market_event_source_status()
    for event in events:
        display_date, display_time, utc = _display_event_datetime(event, zone)
        event["_date_display"], event["_time_display"], event["_utc"] = display_date, display_time, utc
    if not events:
        st.info("No upcoming events are cached for this filter. Refresh official calendars or add an earnings date.")
    else:
        cards = []
        for event in events:
            date_text = escape(event["_date_display"])
            time_text = escape(event["_time_display"])
            if event["all_day"]:
                source_date = date.fromisoformat(event.get("event_date") or event["start_utc"][:10])
                source_today = now.astimezone(ZoneInfo(event.get("source_timezone") or ET)).date()
                day_gap = (source_date - source_today).days
                countdown_text = "today" if day_gap == 0 else f"in {day_gap}d" if day_gap == 1 else f"in {day_gap} days"
            else:
                countdown_text = _format_countdown(event["_utc"], now)
            countdown = escape(countdown_text)
            name = escape(event["name"])
            category_name = escape(event["category"])
            last_verified = event.get("last_verified_at")
            source_status = source_statuses.get(event["provider"], {})
            try:
                verified_age = (now - datetime.fromisoformat(last_verified.replace("Z", "+00:00")).astimezone(timezone.utc)).days
            except (AttributeError, ValueError):
                verified_age = 9999
            stale_after = _stale_after_days(event["provider"])
            manually_entered = event.get("provider", "").startswith("manual_")
            freshness = " · stale" if (not manually_entered and (verified_age > stale_after or source_status.get("status") == "error")) else ""
            status_text = event["status"]
            verification = event.get("source_verification") or "Unverified"
            if event.get("provider", "").startswith("manual_"):
                status_text = ("User confirmed" if status_text == "Confirmed" else status_text)
                status_text += " · Manually entered"
                if verification != "Official source":
                    status_text += " · Unverified source"
            status = escape(status_text + freshness)
            color = CATEGORY_COLORS.get(event["category"], "#9aa0a6")
            why, monitor = _why_it_matters(event)
            event_notes = event.get("notes") or ""
            relevance = f"<p><b>Event notes / relevance:</b> {escape(event_notes)}</p>" if event_notes else ""
            symbols = event.get("related_symbols", [])
            related_holdings = sorted(set(symbols) & holdings_symbols)
            related_workspaces = sorted(set(symbols) & workspace_symbols)
            url = _source_link(event.get("source_url", ""))
            link = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Source ↗</a>' if url else "No source URL provided"
            related = ""
            if related_holdings:
                related += "<p><b>Related holdings:</b> " + escape(", ".join(related_holdings)) + "</p>"
            if related_workspaces:
                related += "<p><b>Research workspaces:</b> " + escape(", ".join(related_workspaces)) + "</p>"
            revisions = list_market_event_revisions(event["event_id"])
            audit_items = []
            for revision in revisions:
                if revision["action"] == "created":
                    continue
                previous, current = revision.get("previous") or {}, revision.get("current") or {}
                old_date = previous.get("start_utc", "")[:16].replace("T", " ")
                new_date = current.get("start_utc", "")[:16].replace("T", " ")
                audit_items.append(f"{revision['action']}: {old_date or 'unknown'} → {new_date or 'unknown'} · {revision['changed_at'][:16]}")
            audit = "<p><b>Recent changes:</b> " + escape("; ".join(audit_items[:4])) + "</p>" if audit_items else ""
            cards.append(
                f'<article class="catalyst-card" style="--accent:{color}">'
                f'<div class="catalyst-meta"><span class="category">{category_name}</span><span class="status">{status}</span></div>'
                f'<div class="event-date">{date_text}</div><h4>{name}</h4>'
                f'<div class="event-time">{time_text} · {countdown}</div>'
                f'<details><summary>Details</summary><p>{escape(why)}</p><p><b>Monitor:</b> {escape(monitor)}</p>'
                f'{relevance}{related}{audit}<p>{link}</p><small>{escape(verification)} · {escape(event.get("source_provider") or event["provider"])}'
                f'{(" · source published " + escape(event["source_published_at"])) if event.get("source_published_at") else ""}'
                f'{(" · checked " + escape(last_verified)) if last_verified else ""}</small></details>'
                '</article>'
            )
        st.html(f"""<style>
        .catalyst-track{{box-sizing:border-box;display:flex;gap:14px;width:100%;max-width:100%;min-width:0;overflow-x:auto;padding:6px 2px 16px;scroll-snap-type:x mandatory;}}
        .catalyst-card{{box-sizing:border-box;flex:0 0 {CALENDAR_CARD_WIDTH}px;min-height:{CALENDAR_CARD_MIN_HEIGHT}px;scroll-snap-align:start;border:1px solid rgba(150,160,175,.22);border-top:3px solid var(--accent);border-radius:10px;background:rgba(35,40,48,.35);padding:{CALENDAR_CARD_PADDING}px;color:inherit;font:inherit;overflow-wrap:anywhere;}}
        .catalyst-meta{{display:flex;justify-content:space-between;gap:8px;font-size:{CALENDAR_META_FONT_SIZE}px;line-height:1.35;color:#c1cad6}}.category{{color:var(--accent);font-weight:650}}.status{{opacity:.9}}
        .event-date{{font-size:{CALENDAR_DATE_FONT_SIZE}px;font-weight:550;margin-top:9px;color:#e0e6ef}}.catalyst-card h4{{font-size:{CALENDAR_TITLE_FONT_SIZE}px;line-height:1.3;margin:5px 0 8px;color:#f2f4f8;overflow-wrap:anywhere}}.event-time{{font-size:{CALENDAR_BODY_FONT_SIZE}px;line-height:1.4;color:#c1cad6}}
        .catalyst-card details{{margin-top:10px;font-size:{CALENDAR_BODY_FONT_SIZE}px;line-height:1.45;color:#d5dbe4}}.catalyst-card summary{{cursor:pointer;color:#c2ccda}}.catalyst-card p{{line-height:1.45;margin:8px 0}}.catalyst-card small{{font-size:{CALENDAR_SECONDARY_FONT_SIZE}px;line-height:1.4;opacity:.82}}.catalyst-card a{{color:#a7c7ff}}
        @media (max-width: 600px){{.catalyst-card{{flex-basis:260px}}}}
        </style><div class="catalyst-track">""" + "".join(cards) + "</div>")

    statuses = list_market_event_source_status()
    if statuses:
        now_local = datetime.now(timezone.utc)
        stale = [name for name, item in statuses.items()
                 if item["status"] != "ok" or not item.get("last_success_at") or
                 (now_local - datetime.fromisoformat(item["last_success_at"].replace("Z", "+00:00"))).days > _stale_after_days(name)]
        if stale:
            st.caption("Calendar source status needs review: " + ", ".join(stale) + " · cached events retained.")
        with st.expander("Calendar data status", expanded=False):
            sources = "\n\n".join(f"**{name}** · {item['status']} · last success {item.get('last_success_at') or 'never'} · {item.get('message') or 'No provider message.'}" for name, item in sorted(statuses.items()))
            st.markdown(sources)
    else:
        with st.expander("Calendar data status", expanded=False):
            st.caption("No calendar sources refreshed yet. Dates are sourced on request; refresh status will appear here.")
    _render_compact_event_import()
    return events


def _render_compact_event_import() -> None:
    with st.expander("Import Events from GPT", expanded=False):
        today = datetime.now(ZoneInfo(ET)).date()
        window = st.selectbox("Discovery range", (30, 60), index=0, format_func=lambda days: f"Next {days} days", key="gpt_event_window")
        coverage = st.selectbox("Catalyst coverage", (
            "Holdings/watchlist + macro", "Holdings + major market catalysts", "Broad market"),
            key="compact_event_coverage")
        start = today
        end = today + timedelta(days=window - 1)
        prompt = build_compact_discovery_prompt(start.isoformat(), end.isoformat(), coverage=coverage)
        if st.button("Copy Catalyst Discovery Prompt", key="copy_compact_catalyst_prompt"):
            st.session_state["show_compact_catalyst_prompt"] = True
        if st.session_state.get("show_compact_catalyst_prompt"):
            st.code(prompt, language="text")
        st.caption("Use this prompt in ChatGPT with Robinhood MCP enabled there. This local app cannot call that connector. Paste EVENT DATE TIME lines only; every imported date remains unverified until reviewed.")
        raw = st.text_area("Paste compact event lines", key="compact_event_import_text", height=90,
                           placeholder="AAPL 11/02 AMC, DE 11/25 BMO\nJOLTS 11/03 10:00\nFOMC 12/09 14:00\nCPI 11/10 08:30")
        if st.button("Parse / Preview", key="parse_compact_event_import"):
            parsed = parse_compact_events(raw, window_start=start, window_end=end)
            st.session_state["compact_event_import_parsed"] = parsed
            st.session_state["compact_event_import_generation"] = st.session_state.get("compact_event_import_generation", 0) + 1
            from dashboard.event_import import preview_event_import
            st.session_state["compact_event_import_preview"] = preview_event_import(
                {"schema_version": 1, "events": parsed["events"]})
            st.session_state.pop("compact_event_import_error", None)
        parsed = st.session_state.get("compact_event_import_parsed")
        preview = st.session_state.get("compact_event_import_preview")
        if parsed:
            if parsed["warnings"]:
                st.caption(" · ".join(parsed["warnings"]))
            if parsed["invalid_lines"]:
                st.error("Some lines need correction. Edit the text above and parse again; valid lines are still shown below.")
                st.dataframe(parsed["invalid_lines"], hide_index=True, width="stretch")
        if st.session_state.get("compact_event_import_error"):
            st.error(st.session_state["compact_event_import_error"])
        if preview is not None:
            if not preview:
                st.info("No valid events parsed yet.")
            else:
                grid = [{"Row": row.get("event", {}).get("compact_line_number", index),
                         "Import": row["include"], "Override conflicting date": False,
                         "Review result": row["classification"], "Category": row["event"]["category"],
                         "Event": row["event"]["name"], "Ticker": row["event"]["ticker"] or "—",
                         "Date": row["event"]["start_date"], "End date": row["event"].get("end_date") or "",
                         "Timing": row["event"].get("earnings_session") or row["event"].get("start_time") or "TBD",
                         "Status": "Unverified · user/GPT entered"} for index, row in enumerate(preview, 1)]
                edited = st.data_editor(grid, hide_index=True, width="stretch",
                    disabled=["Row", "Review result", "Category", "Ticker", "Status", "End date"],
                    column_config={"Row": st.column_config.NumberColumn("Row", format="%d"),
                                   "Import": st.column_config.CheckboxColumn(default=False),
                                   "Override conflicting date": st.column_config.CheckboxColumn(default=False)},
                    key=f"compact_event_import_review_{st.session_state.get('compact_event_import_generation', 0)}")
                if st.button("Approve Import", type="primary", key="approve_compact_event_import"):
                    try:
                        approved_rows = apply_compact_preview_edits(preview, edited,
                            window_start=start.isoformat(), window_end=end.isoformat())
                        result = approve_event_import(approved_rows, original_json=raw)
                        st.session_state["last_compact_event_import_result"] = result
                        st.session_state.pop("compact_event_import_preview", None)
                        st.session_state.pop("compact_event_import_parsed", None)
                        st.rerun()
                    except Exception as error:
                        # User-authored preview data and a transient SQLite error
                        # must never take down the Market page or clear the input.
                        st.session_state["compact_event_import_error"] = (
                            f"Import was not saved. {error.__class__.__name__}: {error}. "
                            "Your pasted text and preview are preserved; correct or exclude the affected row and retry."
                        )
                        st.rerun()
        result = st.session_state.get("last_compact_event_import_result")
        if result:
            st.success(f"Batch {result['batch_id'][:8]} · added {result['inserted']} · updated {result['updated']} · unchanged {result['unchanged']} · conflicts {result['conflicts']} · skipped {result['skipped']}")
        batches = [batch for batch in list_import_batches() if batch["status"] == "active"]
        if batches:
            with st.expander("Rollback an imported batch"):
                batch_labels = {f"{item['created_at']} · {item['batch_id'][:8]}": item["batch_id"] for item in batches}
                selected_batch = st.selectbox("Import batch", list(batch_labels), key="gpt_event_rollback_batch")
                if st.button("Rollback selected batch", key="rollback_gpt_event_batch"):
                    outcome = rollback_event_import(batch_labels[selected_batch])
                    st.info(f"Rolled back {outcome['rolled_back']} record(s); {outcome['conflicts']} changed after import and were left untouched.")
                    st.rerun()


def _render_earnings_manager() -> None:
    configured = list_earnings_watchlist(include_user_symbols=False)
    integrated = list_earnings_watchlist(include_user_symbols=True)
    with st.form("earnings_watchlist_add", clear_on_submit=True):
        cols = st.columns([3, 1])
        symbol = cols[0].text_input("Track company", placeholder="Ticker").strip().upper()
        if cols[1].form_submit_button("Add ticker"):
            try:
                add_earnings_watch_symbol(symbol)
                st.rerun()
            except ValueError as error:
                st.error(str(error))
    if configured:
        remove = st.selectbox("Remove from configured earnings list", configured, key="earnings_remove_symbol")
        if st.button("Remove ticker", key="earnings_remove_button"):
            remove_earnings_watch_symbol(remove)
            st.rerun()
    st.caption("Tracked symbols include configured tickers, holdings, and research workspaces: " + ", ".join(integrated))

    _render_quick_add_earnings()

    with st.expander("Add sourced political or policy date"):
        with st.form("manual_public_event"):
            name = st.text_input("Event name", placeholder="Policy deadline or election date")
            event_date = st.date_input("Event date", value=datetime.now(ZoneInfo(ET)).date(), key="manual_public_date")
            timed = st.checkbox("Scheduled time is known", value=False, key="manual_public_timed")
            event_time = st.time_input("Scheduled time · Eastern", value=datetime.now(ZoneInfo(ET)).replace(hour=8, minute=30).time(), disabled=not timed, key="manual_public_time")
            status = st.selectbox("Date status", ("Confirmed", "Estimated", "Tentative", "Unverified"), key="manual_public_status")
            source_url = st.text_input("Official source URL", key="manual_public_url")
            notes = st.text_input("Notes", key="manual_public_notes")
            if st.form_submit_button("Save sourced date"):
                try:
                    save_manual_public_event({"name": name, "date": event_date.isoformat(),
                                              "time": event_time.strftime("%H:%M") if timed else "",
                                              "source_timezone": ET, "status": status,
                                              "source_url": source_url, "notes": notes})
                    st.success("Event saved locally.")
                except (ValueError, KeyError) as error:
                    st.error(str(error))


def _render_quick_add_earnings() -> None:
    st.markdown("**Quick Add Earnings**")
    st.caption(
        "Enter ticker/date pairs separated by spaces, commas, semicolons, or new lines. "
        "Dates without a year resolve to the next upcoming date; inferred years appear in the preview."
    )
    raw_text = st.text_area(
        "Earnings entries", placeholder="HOOD 10/27 AMC, AAPL 10/28 BMO, MSFT 10/29",
        key="quick_earnings_input", height=82, label_visibility="collapsed",
    )
    if st.button("Parse / Preview", key="quick_earnings_parse"):
        parsed = parse_quick_earnings(raw_text, as_of=datetime.now(ZoneInfo(ET)).date())
        st.session_state["quick_earnings_parsed"] = parsed
        st.session_state["quick_earnings_parse_generation"] = st.session_state.get("quick_earnings_parse_generation", 0) + 1

    last_result = st.session_state.pop("quick_earnings_last_result", None)
    if last_result:
        st.success(
            f"Saved: {last_result['added']} added · {last_result['updated']} updated · "
            f"{last_result['skipped']} skipped · {last_result['rejected']} rejected."
        )
        for detail in last_result.get("details", []):
            st.caption(f"{detail.get('ticker') or 'Input'} {detail.get('date') or ''} · {detail['status']}: {detail['message']}")

    parsed = st.session_state.get("quick_earnings_parsed")
    if not parsed:
        return
    if parsed["errors"]:
        st.warning("Some text needs correction. These parts will not be saved:")
        for error in parsed["errors"]:
            st.caption(f"{error['text'] or '(empty)'} — {error['error']}")
    if not parsed["events"]:
        return

    inferred_years = [f"{item['ticker']} {item['date'][:4]}" for item in parsed["events"] if item["year_inferred"]]
    if inferred_years:
        st.caption("Inferred years · " + ", ".join(inferred_years) + f" · next-upcoming rule anchored to {parsed['as_of']}; review the dates below.")
    st.caption("Preview rows are User-entered / Unverified. Edit ticker, date, timing, or uncheck rows to skip them.")
    preview_rows = [{
        "Accept": True,
        "Ticker": item["ticker"],
        "Date": date.fromisoformat(item["date"]),
        "Timing": item["session"],
        "Status": "User-entered / Unverified",
    } for item in parsed["events"]]
    generation = st.session_state.get("quick_earnings_parse_generation", 0)
    edited = st.data_editor(
        preview_rows, key=f"quick_earnings_preview_{generation}", hide_index=True,
        width="stretch",
        column_config={
            "Accept": st.column_config.CheckboxColumn("Save", default=True),
            "Ticker": st.column_config.TextColumn("Ticker", required=True),
            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD", required=True),
            "Timing": st.column_config.SelectboxColumn("Timing", options=["BMO", "AMC", "TBD"], required=True),
        },
        disabled=["Status"],
    )
    if st.button("Save All", key=f"quick_earnings_save_{generation}", type="primary"):
        accepted = []
        edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else edited
        for row in edited_rows:
            if not row.get("Accept"):
                continue
            accepted.append({"ticker": row.get("Ticker"), "date": row.get("Date"), "session": row.get("Timing")})
        result = save_quick_earnings_events(accepted, original_input=parsed["original_input"])
        result["rejected"] += len(parsed["errors"])
        result["details"].extend({
            "ticker": "Input", "status": "rejected", "message": f"{error['text'] or '(empty)'} — {error['error']}"
        } for error in parsed["errors"])
        st.session_state["quick_earnings_last_result"] = result
        st.session_state.pop("quick_earnings_parsed", None)
        st.rerun()
