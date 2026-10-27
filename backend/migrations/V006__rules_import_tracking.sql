-- V006: track which import last changed an eligibility rule (same as every other imported table).
ALTER TABLE offer_eligibility_rules ADD (import_id NUMBER);
