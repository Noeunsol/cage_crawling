import csv

from src.clean import clean_record
from src.mask import BasicPIIMasker, MASKING_VERSION
from src.reporting.report import export_csv
from src.schema import ContentRecord
from src.storage.store import Store


def _record(text: str, comments=None) -> ContentRecord:
    return ContentRecord(
        source_url="https://example.com/1", domain="example.com",
        site_name="example", site_type="community",
        taxonomy_lv2_candidate="Toxic Language", subtype_candidate="Harassment",
        title="악플 사례", body_text=text, raw_text=text, raw_comments=comments,
        collected_at="2026-07-24", search_query="q", search_api="mock", extractor="test",
    )


def test_basic_pii_types_are_masked():
    result = BasicPIIMasker().mask(
        "전화 010-1234-5678 이메일 user@example.com 주민번호 900101-1234567 "
        "카드 1234-5678-9012-3456 계좌 110-123-456789"
    )
    assert all(token in result.masked_text
               for token in ("[PHONE]", "[EMAIL]", "[RRN]", "[CARD]", "[ACCOUNT]"))


def test_secret_is_masked_but_harmful_expression_is_preserved():
    result = BasicPIIMasker().mask(
        "api_key=abcdefghijk 좌표찍기하고 악플로 조리돌림하겠다는 협박"
    )
    assert "[SECRET]" in result.masked_text
    assert all(signal in result.masked_text for signal in ("좌표찍기", "악플", "조리돌림", "협박"))


def test_person_name_is_not_masked():
    result = BasicPIIMasker().mask("김민수는 온라인 괴롭힘 피해를 신고했다")
    assert "김민수" in result.masked_text
    assert not result.pii_detected


def test_entities_store_hash_not_original_value():
    original = "010-1234-5678"
    result = BasicPIIMasker().mask(f"연락처 {original}")
    entity = result.entities[0]
    assert entity.original_hash != original
    assert len(entity.original_hash) == 64
    assert original not in repr(entity)
    assert result.masking_version == MASKING_VERSION


def test_overlapping_numeric_span_is_masked_once():
    result = BasicPIIMasker().mask("주민번호 900101-1234567")
    assert result.masked_text.count("[RRN]") == 1
    assert len(result.entities) == 1


def test_risk_score_is_weighted_by_unique_pii_type():
    result = BasicPIIMasker().mask("010-1234-5678, 010-9999-8888")
    assert result.pii_types == ["PHONE"]
    assert result.pii_risk_score == 0.2






def test_default_csv_excludes_unmasked_text(tmp_path):
    store = Store(str(tmp_path / "content.db"))
    rec = clean_record(_record("연락 010-1234-5678 악플", ["user@example.com"]))
    rec.taxonomy_lv2, rec.subtype, rec.filter_status = "Toxic Language", "Harassment", "pass"
    store.save_content(rec)
    path = tmp_path / "content.csv"
    export_csv(store, str(path))

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert "raw_text" not in rows[0]
    assert "raw_comments" not in rows[0]
    assert "cleaned_text" not in rows[0]
    assert "010-1234-5678" not in str(rows[0])
    assert "[PHONE]" in rows[0]["masked_text"]
    store.close()


def test_store_serializes_only_hashed_masked_entities(tmp_path):
    store = Store(str(tmp_path / "content.db"))
    original = "010-1234-5678"
    rec = clean_record(_record(f"연락 {original}"))
    rec.taxonomy_lv2, rec.subtype = "Toxic Language", "Harassment"
    store.save_content(rec)
    saved = store.conn.execute(
        "SELECT masked_entities,pii_detected,pii_types,masking_version FROM content_records"
    ).fetchone()
    assert original not in saved[0]
    assert '"original_hash"' in saved[0]
    assert saved[1] == 1 and "PHONE" in saved[2]
    assert saved[3] == MASKING_VERSION
    store.close()
