"""Malformed OSM IDs must not poison otherwise valid SPARQL batches."""
import re
import urllib.error

import pytest

import wikidata_cache as wc


def test_malformed_osm_id_does_not_drop_neighboring_valid_facts(monkeypatch):
    valid = [f"Q{number}" for number in range(100, 140)]
    malformed = "Q17287873;Q17287878"  # Observed in the Netherlands PBF.
    requested = []

    def endpoint(query, **kwargs):
        values = query.split("VALUES ?item {", 1)[1].split("}", 1)[0]
        if malformed in values:
            raise urllib.error.HTTPError("https://example.test", 400, "Bad query", {}, None)
        qids = re.findall(r"wd:(Q[1-9][0-9]*)", values)
        requested.extend(qids)
        return [{"item": {"value": f"http://www.wikidata.org/entity/{qid}"},
                 "itemLabel": {"value": f"Label {qid}"}} for qid in qids]

    monkeypatch.setattr(wc, "_run_sparql", endpoint)
    results = wc.fetch_wikidata_batch(valid[:20] + [malformed] + valid[20:])
    assert set(results) == set(valid)
    assert requested == valid
    assert all(entry["label"] == f"Label {qid}" for qid, entry in results.items())


@pytest.mark.parametrize("qid", ["Q0", "Q01", "Q1;Q2", "Q1\n", "Q١", "Q1 } #", "Q10000000000", "", None, 42])
def test_invalid_ids_never_reach_sparql(monkeypatch, qid):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid IDs reached the endpoint")
    monkeypatch.setattr(wc, "_run_sparql", forbidden)
    assert wc.fetch_wikidata_batch([qid]) == {}


def test_build_filters_cached_extraction_without_mutating_source(monkeypatch, tmp_path, capsys):
    features = {"Q31": {"name": "Belgium"}, "Q1;Q2": {"name": "Ambiguous"},
                "Q32": {"name": "Luxembourg"}}
    wc.save_cache(tmp_path, {"Q31": {"qid": "Q31", "label": "Belgium"}}, features)
    monkeypatch.setattr(wc, "extract_qids_from_pbf", lambda *a, **k: features)
    requested = []

    def properties(qids, **kwargs):
        requested.extend(qids)
        return {qid: {"qid": qid, "label": "Luxembourg"} for qid in qids}

    monkeypatch.setattr(wc, "fetch_wikidata_batch", properties)
    assert wc.build_cache(pbf_path="fixture.pbf", cache_dir=tmp_path, skip_extracts=True) == tmp_path
    assert requested == ["Q32"]
    assert set(wc.load_cache(tmp_path)) == {"Q31", "Q32"}
    assert "Q1;Q2" in features
    assert "malformed" in capsys.readouterr().out.lower()
