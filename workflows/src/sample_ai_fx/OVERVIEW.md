# Weather AI function

`example.sample_ai_fx.uplifting_weather` accepts `location` and optional
`activity`, resolves a location through `accuWeather.locations.search`, then calls
`accuWeather.currentConditions.getByLocationKey` for current weather.

Both tools bind their `apiKey` argument to the SECRET Vari
`example.sample_ai_fx.weather_api_key`. Set its value in Canopy after creation;
never put it in an artefact or environment substitution. Flow resolves the Vari
ID and tool IDs without reading the secret value.

The enabled AI artefact defines the prompt, Responses API model selection,
and strict output schema: `location`, `currentWeather`, `description`,
`recommendedItems`, and `upliftingMessage`. Verify that the target provides the
two AccuWeather functions and the configured model/provider before deployment.

```fish
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_ai_fx --types variables,ai-functions --dry-run
env ENVPATH=workflows/dev.env uv run --locked python cli/flow.py push --paths sample_ai_fx --types variables,ai-functions
```

The runtime test is separately gated because invoking the AI function can incur
provider usage. It needs both opt-ins and an already configured target:

```fish
env RUN_POLY_INTEGRATION=true RUN_AI_INTEGRATION=true uv run --locked --env-file workflows/dev.env pytest workflows/src/sample_ai_fx/tests/test_weather_integration.py
```

Local tests validate the tool/secret bindings and payload schema without invoking
AI or accessing provider credentials.
