from shopassist.retrieval import _clean, _pack, rrf, sections, tokenize

PAGE = {"doc_id": "p", "title": "Refund Policy", "url": "u", "topic": "t", "fetched_at": "", "body": """Intro text that
explains refunds.

To view in your preferred language, locate the country flag.

## Table of Contents

- [Refund Timelines](#a)
- [Pay on Delivery](#b)

## Refund Timelines

| Method | Time |
| --- | --- |
| UPI | Up to 5 Working Days |

### Pay on Delivery

Refunds go by [NEFT](https://very.long/url?with=tracking) to your bank.
"""}


def test_sections_keep_intro_and_heading_path_and_drop_navigation():
    heads = [h for h, _ in sections(PAGE)]
    assert heads == ["Overview", "Refund Timelines", "Refund Timelines > Pay on Delivery"]


def test_clean_drops_boilerplate_unwraps_lines_and_strips_urls():
    text = _clean(dict(sections(PAGE))["Overview"])
    assert text == "Intro text that explains refunds."
    assert _clean("Refunds go by [NEFT](https://x.y/z) to **your** bank.") == "Refunds go by NEFT to your bank."


def test_large_tables_split_by_rows_with_header_repeated():
    table = "| A | B |\n| --- | --- |\n" + "\n".join(f"| row{i} | {'x' * 40} |" for i in range(30))
    parts = _pack(table, max_chars=300)
    assert len(parts) > 1 and all(p.startswith("| A | B |\n| --- | --- |") for p in parts)


def test_rrf_rewards_agreement_between_retrievers():
    fused = dict(rrf([[1, 2, 3], [3, 1, 4]]))
    assert max(fused, key=fused.get) == 1 and fused[3] > fused[2] and fused[4] > 0


def test_tokenize_keeps_compounds_and_parts():
    toks = tokenize("Pay-on-Delivery refunds in 5-7 days")
    assert {"pay-on-delivery", "pay", "delivery", "5-7", "refunds"} <= set(toks) and "in" not in toks
