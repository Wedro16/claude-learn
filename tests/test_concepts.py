from learner import concepts


def test_aliases_resolve_to_one_concept(store):
    a = store.record_concept("Retry with backoff", aliases=["retry logic", "exponential backoff"])
    b = store.record_concept("exponential backoff")
    c = store.record_concept("Retry Logic!")
    assert a["created"] and not b["created"] and not c["created"]
    assert a["concept_id"] == b["concept_id"] == c["concept_id"] == "retry-with-backoff"


def test_fuzzy_match_catches_small_variations(store):
    store.record_concept("dependency injection")
    assert store.record_concept("dependency-injections")["concept_id"] == "dependency-injection"


def test_distinct_concepts_stay_distinct(store):
    a = store.record_concept("SQL join semantics")
    b = store.record_concept("claim-evidence structure", domain="writing")
    assert a["concept_id"] != b["concept_id"]


def test_sensitive_topics_are_rejected_and_not_stored(store):
    result = store.record_concept("parental leave policy")
    assert result["rejected"] == "sensitive"
    assert concepts.find(store.conn, "parental leave policy") is None
    assert store.record_concept("performance review process")["rejected"] == "sensitive"


def test_learning_topics_are_not_flagged_sensitive():
    assert not concepts.is_sensitive("health check endpoints")
    assert not concepts.is_sensitive("code review checklist")


def test_sensitive_relation_rejects_the_whole_record(store):
    result = store.record_concept(
        "feedback writing", relations=[{"concept": "performance review process", "type": "used_with"}]
    )
    assert result["rejected"] == "sensitive"
    assert concepts.find(store.conn, "performance review process") is None
    assert concepts.find(store.conn, "feedback writing") is None


def test_pip_the_package_installer_is_not_sensitive():
    assert not concepts.is_sensitive("pip dependency resolution", "install the package with pip")
    assert concepts.is_sensitive("what happens when you are put on a PIP")
    assert concepts.is_sensitive("preparing for a PIP meeting")
