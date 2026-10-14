"""Every dashboard feature, exercised the way a person would use it."""

import pytest

from tests.e2e.conftest import BASE_URL, SAMPLES

ROBERT_A = {
    "mrn": "0012345",
    "patient_name": "ROBERT MILLER",
    "dob": "03/07/1984",
    "sex": "M",
    "ssn": "912-34-5678",
    "phone": "(555) 201-3344",
    "zip": "10001",
}
BOB_B_SWAPPED = {
    "patient_id": "B-12345",
    "first_name": "Bob",
    "last_name": "MILLER",
    "date_of_birth": "03-07-1984",
    "gender": "Male",
    "mobile": "+1 555-201-3344",
    "pincode": "10001",
}
AMBIGUOUS_C = {
    "hospital_no": "C55503",
    "full_name": "OKAFOR, Amara",
    "birth_date": "03/04/1985",
    "sex_code": "2",
}
UNMAPPED_A = {"mrn": "0012399", "patient_name": "Ana Silva", "dob": "05/05/1975", "sex": "X"}


# ---------- Connecting ----------


def test_a_wrong_key_is_refused_and_a_right_one_opens_the_org(dashboard, org, expect):
    page = dashboard.page
    page.goto(f"{BASE_URL}/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("hospital record")
    page.get_by_label("API key").fill("mvk_000000000000_" + "x" * 43)
    page.get_by_role("button", name="Connect").click()
    expect(page.get_by_role("alert")).to_contain_text("That key was not accepted")

    page.get_by_label("API key").fill(org.keys["admin"])
    page.get_by_role("button", name="Connect").click()
    expect(page.locator("#who")).to_have_text(f"{org.name}, admin key")
    expect(page.get_by_role("navigation", name="Sections")).to_be_visible()


def test_the_key_survives_a_reload_and_disconnect_forgets_it(dashboard, org, expect):
    dashboard.open(org.keys["reader"])
    dashboard.page.reload()
    expect(dashboard.page.locator("#who")).to_have_text(f"{org.name}, reader key")
    dashboard.page.get_by_role("button", name="Disconnect").click()
    expect(dashboard.page.get_by_label("API key")).to_be_visible()
    dashboard.page.reload()
    expect(dashboard.page.get_by_label("API key")).to_be_visible()


# ---------- Uploading ----------


def test_choosing_a_file_uploads_it_and_shows_the_job(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "upload")
    dashboard.page.locator("#upload-file").set_input_files(str(SAMPLES / "source-a-patients.csv"))
    job = dashboard.page.locator("#jobs .job").first
    expect(job).to_contain_text("source-a-patients.csv")
    expect(job).to_contain_text("Done, some rows quarantined", timeout=60_000)
    expect(job).to_contain_text("37 normalized")
    expect(job).to_contain_text("3 quarantined")


def test_a_sample_uploads_with_one_click(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "upload")
    dashboard.page.locator(".samples li", has_text="source-b-patients.csv").get_by_role(
        "button", name="Upload"
    ).click()
    job = dashboard.page.locator("#jobs .job").first
    expect(job).to_contain_text("source-b, Patient")
    expect(job).to_contain_text("20 normalized", timeout=60_000)


def test_dropping_a_file_uploads_it(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "upload")
    dashboard.page.select_option("#upload-source", "source-c")
    dashboard.page.evaluate(
        """async () => {
            const text = await (await fetch("/ui/samples/source-c-patients.csv")).text();
            const transfer = new DataTransfer();
            transfer.items.add(new File([text], "dropped.csv", { type: "text/csv" }));
            const drop = new DragEvent("drop", { dataTransfer: transfer, bubbles: true });
            document.querySelector(".drop").dispatchEvent(drop);
        }"""
    )
    job = dashboard.page.locator("#jobs .job").first
    expect(job).to_contain_text("dropped.csv")
    expect(job).to_contain_text("source-c, Patient")
    expect(job).to_contain_text("Done", timeout=60_000)


def test_the_columns_follow_the_chosen_source(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "upload")
    dashboard.page.select_option("#upload-source", "source-b")
    expect(dashboard.page.locator(".columns-list")).to_contain_text("date_of_birth")
    expect(dashboard.page.locator(".aside")).to_contain_text("America/Chicago")


# ---------- Trying one record ----------


def test_checking_a_record_shows_each_conversion_and_saves_nothing(dashboard, org, expect):
    dashboard.open(org.keys["admin"], "try")
    dashboard.page.get_by_role("button", name="Check without saving").click()
    expect(dashboard.page.locator(".success")).to_contain_text("This record is valid.")
    birth = dashboard.page.locator(".slip", has_text="Birth date")
    expect(birth.locator(".slip-raw")).to_have_text("03/07/1984")
    expect(birth.locator(".slip-out")).to_have_text("1984-03-07")
    assert org.get("/metrics")["by_source"] == {}


def test_an_ambiguous_date_is_explained(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "try")
    dashboard.page.select_option("#try-source", "source-c")
    expect(dashboard.page.locator("#try-help")).to_contain_text("ambiguous")
    dashboard.page.get_by_role("button", name="Check without saving").click()
    expect(dashboard.page.locator(".problem")).to_contain_text("would be quarantined")
    expect(dashboard.page.locator(".problem")).to_contain_text("DATE_AMBIGUOUS")


def test_decimals_are_shown_exactly_as_sent(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "try")
    dashboard.page.select_option("#try-source", "source-b")
    dashboard.page.select_option("#try-type", "Observation")
    dashboard.page.get_by_role("button", name="Check without saving").click()
    expect(dashboard.page.locator(".slip", has_text="Value").locator(".slip-out")).to_have_text(
        "5.50"
    )


def test_saving_a_record_then_opening_its_lineage(dashboard, org, expect):
    dashboard.open(org.keys["admin"], "try")
    dashboard.page.get_by_role("button", name="Save record").click()
    expect(dashboard.page.locator(".success")).to_contain_text("Saved and normalized.")
    dashboard.page.get_by_role("link", name="Open the record and its lineage").click()
    expect(dashboard.page.get_by_role("heading", level=2)).to_have_text("Patient from source-a")
    expect(dashboard.page.get_by_text("Current version")).to_be_visible()
    # Source A sends the whole name in one column; the canonical name is split from it.
    name = dashboard.page.locator(".slip", has_text="patient_name")
    expect(name.locator(".slip-raw")).to_have_text("ROBERT MILLER")
    expect(name.locator(".slip-out")).to_contain_text("family: Miller")
    dashboard.page.locator("summary", has_text="Exactly what the source sent").click()
    expect(dashboard.page.locator("pre.json").first).to_contain_text('"dob": "03/07/1984"')


def test_saving_a_bad_record_quarantines_it(dashboard, org, expect):
    dashboard.open(org.keys["ingest"], "try")
    dashboard.page.select_option("#try-source", "source-c")
    dashboard.page.get_by_role("button", name="Save record").click()
    expect(dashboard.page.get_by_role("alert")).to_contain_text("422")
    expect(dashboard.page.get_by_role("alert")).to_contain_text("DATE_AMBIGUOUS")
    expect(dashboard.page.get_by_text("The record was kept in")).to_be_visible()


# ---------- Quarantine ----------


def test_quarantine_rows_expand_and_filter(dashboard, org, expect):
    assert org.post_record("source-c", "Patient", AMBIGUOUS_C).status_code == 422
    assert org.post_record("source-a", "Patient", UNMAPPED_A).status_code == 422
    dashboard.open(org.keys["reader"], "quarantine")
    rows = dashboard.page.locator("tbody tr.is-clickable")
    expect(rows).to_have_count(2)
    dashboard.page.locator("tbody tr.is-clickable", has_text="C55503").click()
    expect(dashboard.page.locator("tr.detail:not([hidden])")).to_contain_text("column birth_date")
    dashboard.page.select_option("#q-source", "source-a")
    dashboard.page.get_by_role("button", name="Show records").click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("CODE_UNMAPPED")
    expect(
        dashboard.page.get_by_role("button", name="Reprocess matching open records")
    ).to_be_hidden()


def test_reprocessing_needs_a_filter_then_runs_a_job(dashboard, org, expect):
    assert org.post_record("source-a", "Patient", UNMAPPED_A).status_code == 422
    dashboard.open(org.keys["admin"], "quarantine")
    reprocess = dashboard.page.get_by_role("button", name="Reprocess matching open records")
    reprocess.click()
    expect(dashboard.page.get_by_text("Choose a source or a reason first")).to_be_visible()
    dashboard.page.get_by_label("Reason").fill("CODE_UNMAPPED")
    reprocess.click()
    job = dashboard.page.locator(".job")
    expect(job).to_contain_text("Reprocessing 1 open record(s)")
    # Same mapping, same result: the record stays in quarantine.
    expect(job).to_contain_text("Done, some rows quarantined", timeout=60_000)


# ---------- Matching ----------


def test_review_queue_decision(dashboard, org, expect):
    org.post_record(
        "source-a",
        "Patient",
        {
            "mrn": "0012398",
            "patient_name": "Priya Sharma",
            "dob": "11/23/1990",
            "sex": "F",
            "zip": "60601",
        },
    )
    org.post_record(
        "source-c",
        "Patient",
        {
            "hospital_no": "C55502",
            "full_name": "SHARMA, Priya",
            "birth_date": "1990-11-23",
            "sex_code": "2",
            "postcode": "60601",
        },
    )
    dashboard.open(org.keys["admin"], "review")
    card = dashboard.page.locator("article.pair")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("Needs a person")
    expect(card.locator(".facts")).to_contain_text("Birth date")
    card.get_by_role("button", name="Different people").click()
    expect(card.locator(".pair-head .mark")).to_have_text("Different people")
    expect(card.locator(".success")).to_contain_text("Kept apart")
    dashboard.page.select_option("#r-status", "different")
    expect(dashboard.page.locator("article.pair")).to_have_count(1)


def test_linked_patients_and_their_conflict_show_in_lookup(dashboard, org, expect):
    a = org.post_record("source-a", "Patient", ROBERT_A).json()["record"]["id"]
    org.post_record("source-b", "Patient", BOB_B_SWAPPED)
    org.wait_until(lambda: len(org.get(f"/v1/patients/{a}/matches")["members"]) == 2)
    dashboard.open(org.keys["admin"], f"lookup/{a}")
    expect(
        dashboard.page.get_by_role("heading", name="Same patient in other records")
    ).to_be_visible()
    expect(dashboard.page.locator("#main")).to_contain_text("source-b B-12345")
    expect(dashboard.page.locator(".findings")).to_contain_text("Birth dates disagree")
    dashboard.go("overview")
    expect(dashboard.page.get_by_role("heading", name="Birth dates disagree (1)")).to_be_visible()
    expect(dashboard.page.locator(".stat-line")).to_contain_text("1linked as the same person")


def test_a_reader_cannot_decide_matches(dashboard, org, expect):
    org.post_record(
        "source-a",
        "Patient",
        {"mrn": "0012398", "patient_name": "Priya Sharma", "dob": "11/23/1990", "sex": "F"},
    )
    org.post_record(
        "source-c",
        "Patient",
        {
            "hospital_no": "C55502",
            "full_name": "SHARMA, Priya",
            "birth_date": "1990-11-23",
            "sex_code": "2",
        },
    )
    dashboard.open(org.keys["reader"], "review")
    expect(dashboard.page.locator("article.pair")).to_have_count(1)
    expect(dashboard.page.get_by_role("button", name="Same person")).to_have_count(0)
    expect(dashboard.page.get_by_text("Recording a decision needs an admin key.")).to_be_visible()


# ---------- Look-up, roles, isolation ----------


def test_lookup_explains_a_malformed_id(dashboard, org, expect):
    dashboard.open(org.keys["reader"], "lookup/not-an-id")
    expect(dashboard.page.locator(".problem")).to_contain_text("That is not a record id")


def test_another_org_cannot_open_the_record(dashboard, org, other_org, expect):
    record_id = org.post_record("source-a", "Patient", ROBERT_A).json()["record"]["id"]
    dashboard.open(other_org.keys["admin"], f"lookup/{record_id}")
    expect(dashboard.page.locator(".problem")).to_have_text(
        "No record with this id in your organisation."
    )


def test_each_role_sees_what_it_may_do(dashboard, org, expect):
    dashboard.open(org.keys["ingest"])
    expect(dashboard.page.get_by_text("can send records but not read them back")).to_be_visible()
    dashboard.page.get_by_role("button", name="Disconnect").click()
    dashboard.open(org.keys["reader"], "upload")
    expect(dashboard.page.get_by_text("Uploading needs an ingest or admin key.")).to_be_visible()
    expect(dashboard.page.locator("#upload-file")).to_be_disabled()
    expect(dashboard.page.locator(".samples button")).to_have_count(0)
    dashboard.go("try")
    expect(dashboard.page.get_by_role("button", name="Save record")).to_be_disabled()
    dashboard.go("overview")
    expect(dashboard.page.get_by_text("Counts by source need an admin key.")).to_be_visible()


def test_overview_counts_add_up(dashboard, org, expect):
    org.post_record("source-a", "Patient", ROBERT_A)
    org.post_record("source-c", "Patient", AMBIGUOUS_C)
    dashboard.open(org.keys["admin"])
    table = dashboard.page.locator("table")
    expect(table.locator("tbody tr", has_text="source-a")).to_contain_text("1")
    expect(table.locator("tfoot")).to_contain_text("All sources")
    footer = table.locator("tfoot td").all_inner_texts()
    assert footer[:5] == ["All sources", "2", "1", "0", "1"]


# ---------- Every page, every size ----------

SECTIONS = ["overview", "upload", "try", "quarantine", "review", "lookup"]


@pytest.mark.parametrize("dashboard", [{"width": 390, "height": 844}], indirect=True)
def test_phone_layout_never_scrolls_sideways(dashboard, org, expect):
    org.post_record("source-c", "Patient", AMBIGUOUS_C)
    dashboard.open(org.keys["admin"])
    for section in SECTIONS:
        dashboard.go(section)
        expect(dashboard.page.locator(f'[data-tab="{section}"]')).to_have_attribute(
            "aria-current", "page"
        )
        overflow = dashboard.page.evaluate(
            "document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 1, f"{section} scrolls sideways by {overflow}px"


def test_dark_mode_and_every_page_load_cleanly(dashboard, org, expect):
    dashboard.page.emulate_media(color_scheme="dark")
    dashboard.open(org.keys["admin"])
    background = dashboard.page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == "rgb(18, 23, 28)"
    for section in SECTIONS:
        dashboard.go(section)
        expect(dashboard.page.get_by_role("heading", level=1)).to_be_visible()
    # Browser errors and CSP violations on any page fail the test in the fixture.
