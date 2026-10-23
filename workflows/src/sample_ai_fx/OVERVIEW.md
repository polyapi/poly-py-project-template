# Weather AI function

`example.sample_ai_fx.uplifting_weather` accepts a city or town in `location`
and optional `activity`. It calls `searchLocations` for coordinates, then
`getCurrentWeather` for current conditions, and returns practical guidance.
Both tools use [Open-Meteo](https://open-meteo.com/), with no weather account,
API key, or trial needed for evaluation.

The public API supports evaluation and non-commercial use. Review
[Open-Meteo's terms and commercial plans](https://open-meteo.com/en/pricing)
before production use. The target still needs the AI provider/model selected in
[the AI artefact](artefacts/ai-functions/uplifting_weather.json); AI execution can
incur provider charges. Configure the tenant provider before running the AI test.

## Configure the AI provider

The sample uses OpenAI through the PolyAPI tenant configuration:

1. Have a tenant administrator store the OpenAI API key as a **SECRET string
   Vari** in the selected tenant. Keep shared provider configuration outside
   this sample's disposable namespace.
2. Set the tenant's `OpenAITenantApiKey` configuration to that Vari's UUID.
   The value is the variable ID, not the provider key. The administrative API
   is `PATCH /tenants/{tenantId}/config-variables/OpenAITenantApiKey` with JSON
   body `{"value":"<SECRET_VARI_UUID>"}`. It requires a user-bound PolyAPI key
   with `ManageTenant` permission and a current `x-otp` MFA code.
3. Confirm the OpenAI project has API credits and access to the configured model.

Keep the provider key in Vari; it does not belong in the local environment file
or AI artefact. The weather tools themselves need no credentials.

A missing tenant configuration returns `403`. A provider failure can appear to
the caller as a generic `500`; inspect the AI execution logs and correlate their
execution ID and timestamp with gateway logs. An upstream `401` indicates an
invalid key, while a `429` stating no credits remain requires billing attention.
Do not retry that quota failure until the provider account is funded or the
administrator replaces the Vari value with a funded project's key. Do not copy
provider key fragments from logs into reports.

## Train the weather tools

Configure the target and replace `example` with your namespace as described in
the root README. From the repository root:

```bash
ENVPATH=workflows/dev.env uv run --locked python -m cli.operations train-openapi workflows/src/sample_ai_fx/artefacts/specs --context example.sample_ai_fx --dry-run --disable-ai
ENVPATH=workflows/dev.env uv run --locked python -m cli.operations train-openapi workflows/src/sample_ai_fx/artefacts/specs --context example.sample_ai_fx --disable-ai
```

The bundled [geocoding spec](artefacts/specs/geocoding_api.json) and
[weather spec](artefacts/specs/weather_api.json) define the exact tool names and
arguments used by the AI artefact. PolyAPI translates the provider query name
`temperature_unit` to the generated argument `temperatureUnit`; the binding uses
that generated name. `--disable-ai` skips optional training-time
metadata enrichment to preserve those contracts; it does not disable the AI
sample. Training saves `.poly.json` models next to the specs and regenerates the
Python SDK. To retrain a saved model, use `train-model` as documented in the
[Flow reference](../../../cli/README.md#openapi-training-cleanup-and-logs).

## Deploy and invoke

The tools and AI function live in the same project namespace. The AI supplies
only the location search text and returned coordinates; fixed argument bindings
select public hosts, Celsius, and the required weather fields. No weather Vari
or provider secret is required.

```bash
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_ai_fx --types ai-functions --dry-run
ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_ai_fx --types ai-functions
```

The structured result contains `location`, `currentWeather`, `description`,
`recommendedItems`, `upliftingMessage`, and `attribution`. Display the attribution
with the result: weather data comes from Open-Meteo under CC BY 4.0 and location
data from GeoNames. Recommendations are AI-generated. Current conditions are
model-based estimates, not station observations or official alerts.

If the tools find no location or usable weather, the prompt requests
`currentWeather: null`, an explanation, and no recommended items. Execution or
provider errors can still propagate to the caller; callers should handle them.

## Verify

Local tests validate the tool/spec bindings and output contract without network
calls or provider credentials:

```bash
uv run --locked pytest workflows/src/sample_ai_fx/tests/test_weather_contract.py
```

The deployed test calls the generated Python SDK. It needs both explicit opt-ins
because the AI call can incur provider usage:

```bash
RUN_POLY_INTEGRATION=true RUN_AI_INTEGRATION=true uv run --locked --env-file workflows/dev.env pytest workflows/src/sample_ai_fx/tests/test_weather_integration.py
```
