-- Record the Model Studio service deployment scope separately from Gamas'
-- production/live environment marker. Region + service-scope determines
-- Alibaba free-quota eligibility; NULL remains fail-closed until an admin
-- verifies the exact account/model deployment in Alibaba's console.
ALTER TABLE ai_provider_settings ADD COLUMN service_deployment_scope TEXT;
