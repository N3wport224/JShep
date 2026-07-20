"""A/B campaign variant assignment and per-variant metric tracking."""
from app.models import Campaign, CampaignVariant, Lead
from app.services.campaigns import assign_variant, record_positive, record_reply, record_sent


def _make_campaign(sync_db, weights=(1, 1)):
    campaign = Campaign(name=f"campaign-{id(weights)}-{weights}")
    sync_db.add(campaign)
    sync_db.flush()
    variants = []
    for i, weight in enumerate(weights):
        v = CampaignVariant(campaign_id=campaign.id, label=chr(65 + i), weight=weight, prompt_hint=f"hint-{i}")
        sync_db.add(v)
        variants.append(v)
    sync_db.commit()
    for v in variants:
        sync_db.refresh(v)
    sync_db.refresh(campaign)
    return campaign, variants


def _make_lead(sync_db, campaign_id=None, email=None):
    import uuid

    lead = Lead(
        company_name="Acme",
        contact_name="Jane",
        email=email or f"jane-{uuid.uuid4().hex[:8]}@acme.example.com",
        campaign_id=campaign_id,
    )
    sync_db.add(lead)
    sync_db.commit()
    sync_db.refresh(lead)
    return lead


def test_assign_variant_returns_none_without_campaign(sync_db):
    lead = _make_lead(sync_db)
    assert assign_variant(sync_db, lead) is None


def test_assign_variant_picks_one_of_the_campaign_variants(sync_db):
    campaign, variants = _make_campaign(sync_db)
    lead = _make_lead(sync_db, campaign_id=campaign.id)

    chosen = assign_variant(sync_db, lead)

    assert chosen is not None
    assert chosen.id in {v.id for v in variants}
    assert lead.variant_id == chosen.id


def test_assign_variant_is_deterministic_and_sticky(sync_db):
    campaign, variants = _make_campaign(sync_db)
    lead = _make_lead(sync_db, campaign_id=campaign.id)

    first = assign_variant(sync_db, lead)
    second = assign_variant(sync_db, lead)  # already assigned - must return the same one

    assert first.id == second.id


def test_assign_variant_distributes_across_many_leads(sync_db):
    campaign, variants = _make_campaign(sync_db, weights=(1, 1))
    label_counts = {"A": 0, "B": 0}
    for _ in range(40):
        lead = _make_lead(sync_db, campaign_id=campaign.id)
        chosen = assign_variant(sync_db, lead)
        label_counts[chosen.label] += 1

    # With equal weights and 40 leads, expect both variants to get picked at
    # least a few times (deterministic hashing, not literally 50/50).
    assert label_counts["A"] > 0
    assert label_counts["B"] > 0


def test_record_sent_reply_positive_update_variant_counters(sync_db):
    campaign, variants = _make_campaign(sync_db)
    lead = _make_lead(sync_db, campaign_id=campaign.id)
    assign_variant(sync_db, lead)
    variant = lead.variant

    record_sent(sync_db, lead)
    record_sent(sync_db, lead)
    record_reply(sync_db, lead)
    record_positive(sync_db, lead)

    sync_db.refresh(variant)
    assert variant.sent_count == 2
    assert variant.reply_count == 1
    assert variant.positive_count == 1
    assert variant.reply_rate == 0.5
    assert variant.positive_rate == 0.5


def test_record_metrics_noop_for_lead_without_variant(sync_db):
    lead = _make_lead(sync_db)
    # Should not raise even though the lead has no campaign/variant.
    record_sent(sync_db, lead)
    record_reply(sync_db, lead)
    record_positive(sync_db, lead)


def test_variant_rates_zero_before_any_sends(sync_db):
    campaign, variants = _make_campaign(sync_db)
    assert variants[0].open_rate == 0.0
    assert variants[0].reply_rate == 0.0
    assert variants[0].positive_rate == 0.0
