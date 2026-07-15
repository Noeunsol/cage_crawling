"""end-to-end 검증. 프레임워크 최소, assert 기반."""
import sqlite3

from src import pipeline
from src.clean import mask_pii


def test_pipeline_stores_pass_record(tmp_path):
    db = tmp_path / "content.db"
    report = pipeline.run(
        taxonomy_config="configs/taxonomy_policy.yaml",
        site_config="configs/site_policy.yaml",
        settings_config="configs/crawler_settings.yaml",
        db_path=str(db),
        report_path=str(tmp_path / "report.json"),
    )
    conn = sqlite3.connect(db)

    # (a) Toxic Language / Cyberbullying pass 레코드 ≥1, 최소 필수 필드 채워짐
    rows = conn.execute(
        "SELECT source_url, title, body_text FROM content_records "
        "WHERE taxonomy_lv2='Toxic Language' AND subtype='Cyberbullying' AND filter_status='pass'"
    ).fetchall()
    assert rows, "pass 상태 ContentRecord가 최소 1건 있어야 한다"
    for source_url, title, body in rows:
        assert source_url and title and body

    # (c) quality fail은 content_records에 없고 filter_logs에 기록
    fails = conn.execute(
        "SELECT COUNT(*) FROM filter_logs WHERE stage='quality' AND status='fail'"
    ).fetchone()[0]
    # quality fail 발생 시 content_records에는 저장되지 않았음을 보장 (source_url 교집합 0)
    orphan = conn.execute(
        "SELECT COUNT(*) FROM content_records c "
        "JOIN filter_logs f ON c.source_url=f.source_url "
        "WHERE f.stage='quality' AND f.status='fail'"
    ).fetchone()[0]
    assert orphan == 0, "quality 탈락 레코드가 content_records에 저장되면 안 된다"

    assert report["stored_records"] >= 1
    conn.close()


def test_pii_masking():
    text = "연락처 010-1234-5678, 이메일 a.b@example.com, 주민번호 900101-1234567"
    masked = mask_pii(text)
    assert "[PHONE]" in masked
    assert "[EMAIL]" in masked
    assert "[RRN]" in masked
    assert "010-1234-5678" not in masked
    assert "900101-1234567" not in masked
