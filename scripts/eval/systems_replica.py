"""Replica-lag fixtures: a consumer calls a new source endpoint that reads only the read replica.

Variants (one Git repository each):
* replica            - a sibling source endpoint already confirms replica misses against the primary.
* replica_nofb       - negative control: no such mechanism exists anywhere.
* replica_transient  - control: the source contract documents 503 failover and a shared client retry policy.
* replica_consumer   - only the consumer repository; the source service is another team's repository.
Tickets never mention the existing mechanism.
"""
from types import SimpleNamespace

README = """# Pricing platform

.NET 8 services, one Azure SQL database per service.

| Service | Owns |
|---|---|
| `src/Rates.Api` | item rates (price per pricing unit). Writes go to the primary database; reads are served from a read replica (`RatesReplica` connection). |
| `src/Quotes.Api` | customer quotes and bundle pricing |

See docs/architecture.md and docs/adr/.
"""

ARCH = """# Architecture

* Rates is the system of record for rates. Other services never read the Rates database; they
  use the Rates HTTP API (ADR-004).
* Rates reads use the read replica to keep load off the primary. Azure SQL geo-replication lag is
  usually below a second but has reached minutes under load.
* Quotes computes the rate of a newly added component (ComponentCostCalculator) and stores it
  in Rates (`PUT /rates/{sku}`) before pricing bundles that contain it.
"""

ADR4 = """# ADR-004 Quotes reads rates through the Rates API

Status: accepted (2026-10-20)

Quotes used to read the Rates database directly through a shared view. Rates owns the data and
its consistency, so Quotes now calls the Rates API. The shared view is removed after migration.
"""

STORES = """using Microsoft.Data.SqlClient;

namespace Rates.Api.Data;

public sealed record Rate(string Sku, decimal Amount, string Currency, int PerQuantity, string Unit);

/// <summary>Read replica (connection "RatesReplica").</summary>
public interface IRateReadStore
{
    Task<Rate?> FindAsync(string sku, CancellationToken ct);
    Task<IReadOnlyList<Rate>> FindManyAsync(IReadOnlyCollection<string> skus, CancellationToken ct);
}

/// <summary>Primary database (connection "Rates"). Used for writes.</summary>
public interface IRatePrimaryStore
{
    Task<Rate?> FindAsync(string sku, CancellationToken ct);
    Task<IReadOnlyList<Rate>> FindManyAsync(IReadOnlyCollection<string> skus, CancellationToken ct);
    Task UpsertAsync(Rate rate, CancellationToken ct);
}
"""

DTO = """using Rates.Api.Data;

namespace Rates.Api.Features;

public sealed record RateDto(string Sku, decimal Amount, string Currency, int PerQuantity, string Unit)
{
    public static RateDto From(Rate r) => new(r.Sku, r.Amount, r.Currency, r.PerQuantity, r.Unit);
}
"""

GET_RATE_V1 = """using Rates.Api.Data;

namespace Rates.Api.Features;

public sealed class GetRateHandler(IRateReadStore replica)
{
    public async Task<RateDto?> HandleAsync(string sku, CancellationToken ct)
    {
        var rate = await replica.FindAsync(sku, ct);
        return rate is null ? null : RateDto.From(rate);
    }
}
"""

GET_RATE_V2 = """using Rates.Api.Data;

namespace Rates.Api.Features;

public sealed class GetRateHandler(IRateReadStore replica, IRatePrimaryStore primary, ILogger<GetRateHandler> log)
{
    public async Task<RateDto?> HandleAsync(string sku, CancellationToken ct)
    {
        var rate = await replica.FindAsync(sku, ct);
        if (rate is null)
        {
            // The replica can trail the primary; confirm a miss there before answering 404.
            rate = await primary.FindAsync(sku, ct);
            if (rate is not null)
                log.LogInformation("Rate {Sku} read from primary", sku);
        }
        return rate is null ? null : RateDto.From(rate);
    }
}
"""

PUT_RATE = """using Rates.Api.Data;

namespace Rates.Api.Features;

public sealed class PutRateHandler(IRatePrimaryStore primary)
{
    public Task HandleAsync(RateDto dto, CancellationToken ct)
        => primary.UpsertAsync(new Rate(dto.Sku, dto.Amount, dto.Currency, dto.PerQuantity, dto.Unit), ct);
}
"""

COMPONENT_RATES = """using Rates.Api.Data;

namespace Rates.Api.Features;

/// <summary>Rates of several items in one call (bundle pricing).</summary>
public sealed class GetComponentRatesHandler(IRateReadStore replica)
{
    public async Task<IReadOnlyList<RateDto>> HandleAsync(IReadOnlyCollection<string> skus, CancellationToken ct)
    {
        var rates = await replica.FindManyAsync(skus, ct);
        return rates.Select(RateDto.From).ToList();
    }
}
"""

RATES_PROGRAM_V1 = """using Rates.Api.Features;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddRateStores(builder.Configuration);
builder.Services.AddScoped<GetRateHandler>();
builder.Services.AddScoped<PutRateHandler>();
var app = builder.Build();

app.MapGet("/rates/{sku}", async (string sku, GetRateHandler h, CancellationToken ct)
    => await h.HandleAsync(sku, ct) is { } r ? Results.Ok(r) : Results.NotFound());
app.MapPut("/rates/{sku}", async (string sku, RateDto dto, PutRateHandler h, CancellationToken ct)
    => { await h.HandleAsync(dto with { Sku = sku }, ct); return Results.NoContent(); });

app.Run();
"""

RATES_PROGRAM_V2 = RATES_PROGRAM_V1.replace(
    "builder.Services.AddScoped<PutRateHandler>();\n",
    "builder.Services.AddScoped<PutRateHandler>();\nbuilder.Services.AddScoped<GetComponentRatesHandler>();\n").replace(
    "\napp.Run();", """
app.MapGet("/rates/components", async (string skus, GetComponentRatesHandler h, CancellationToken ct) =>
{
    var wanted = skus.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
    var found = await h.HandleAsync(wanted, ct);
    var missing = wanted.Except(found.Select(r => r.Sku)).ToList();
    return missing.Count == 0
        ? Results.Ok(found)
        : Results.Problem(statusCode: 404, title: "Rate not found", detail: string.Join(",", missing));
});

app.Run();""")

QUOTES_RATES_DB_CLIENT = """using Dapper;
using Microsoft.Data.SqlClient;

namespace Quotes.Api.Clients;

/// <summary>Reads rates through the shared Rates view (vw_Rates).</summary>
public sealed class RatesClient(SqlConnection ratesView, HttpClient http)
{
    public async Task<IReadOnlyDictionary<string, RateDto>> GetComponentRatesAsync(IReadOnlyCollection<string> skus, CancellationToken ct)
    {
        var rows = await ratesView.QueryAsync<RateDto>(
            "SELECT Sku, Amount, Currency, PerQuantity, Unit FROM vw_Rates WHERE Sku IN @skus", new { skus });
        return rows.ToDictionary(r => r.Sku);
    }

    public async Task PutRateAsync(RateDto rate, CancellationToken ct)
        => (await http.PutAsJsonAsync($"/rates/{rate.Sku}", rate, ct)).EnsureSuccessStatusCode();
}

public sealed record RateDto(string Sku, decimal Amount, string Currency, int PerQuantity, string Unit);
"""

QUOTES_RATES_API_CLIENT = """using System.Net;

namespace Quotes.Api.Clients;

/// <summary>Rates HTTP API client (ADR-004).</summary>
public sealed class RatesClient(HttpClient http)
{
    public async Task<IReadOnlyDictionary<string, RateDto>> GetComponentRatesAsync(IReadOnlyCollection<string> skus, CancellationToken ct)
    {
        var response = await http.GetAsync("/rates/components?skus=" + Uri.EscapeDataString(string.Join(",", skus)), ct);
        if (response.StatusCode == HttpStatusCode.NotFound)
            return new Dictionary<string, RateDto>();
        response.EnsureSuccessStatusCode();
        var rates = await response.Content.ReadFromJsonAsync<List<RateDto>>(ct) ?? [];
        return rates.ToDictionary(r => r.Sku);
    }

    public async Task PutRateAsync(RateDto rate, CancellationToken ct)
        => (await http.PutAsJsonAsync($"/rates/{rate.Sku}", rate, ct)).EnsureSuccessStatusCode();
}

public sealed record RateDto(string Sku, decimal Amount, string Currency, int PerQuantity, string Unit);
"""

PRICER = """using Quotes.Api.Clients;
using Quotes.Api.Data;

namespace Quotes.Api.Pricing;

public sealed class PricingException(string message) : Exception(message);

public sealed class BundlePricer(RatesClient rates, ComponentCostCalculator calculator, QuotesDbContext db)
{
    public async Task<decimal> PriceAsync(Bundle bundle, CancellationToken ct)
    {
        foreach (var component in bundle.Components.Where(c => c.IsNew))
        {
            var rate = calculator.Calculate(component);
            await rates.PutRateAsync(rate, ct);
            db.QuoteResults.Add(new QuoteResult { BundleId = bundle.Id, Sku = component.Sku, Amount = rate.Amount });
        }
        await db.SaveChangesAsync(ct);

        var known = await rates.GetComponentRatesAsync(bundle.Components.Select(c => c.Sku).ToList(), ct);
        decimal total = 0;
        foreach (var component in bundle.Components)
        {
            if (!known.TryGetValue(component.Sku, out var rate))
                throw new PricingException($"Item {component.Sku} has no rate");
            total += rate.Amount * component.Quantity / rate.PerQuantity;
        }
        return total;
    }
}
"""

QUOTE_RESULT = """namespace Quotes.Api.Data;

/// <summary>Audit of amounts calculated while pricing a bundle (amount only; no currency or unit).</summary>
public sealed class QuoteResult
{
    public long Id { get; set; }
    public Guid BundleId { get; set; }
    public string Sku { get; set; } = "";
    public decimal Amount { get; set; }
}
"""

QUOTES_PROGRAM = """using Quotes.Api.Clients;
using Quotes.Api.Pricing;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddDbContext<Quotes.Api.Data.QuotesDbContext>();
builder.Services.AddHttpClient<RatesClient>(c => c.BaseAddress = new Uri(builder.Configuration["Rates:Url"]!));
builder.Services.AddScoped<ComponentCostCalculator>();
builder.Services.AddScoped<BundlePricer>();
var app = builder.Build();
app.MapPost("/quotes/bundles/{id:guid}/price", async (Guid id, BundlePricer pricer, Quotes.Api.Data.QuotesDbContext db, CancellationToken ct)
    => Results.Ok(await pricer.PriceAsync(await db.LoadBundleAsync(id, ct), ct)));
app.Run();
"""

# Control: the source contract documents transient 503s and a shared retry policy for clients.
FAILOVER_CONTRACT = """# Rates API contract for clients

* Reads are idempotent GETs.
* During the nightly primary failover test (02:00-02:05 UTC) and unplanned failovers, Rates returns
  `503 Service Unavailable` with `Retry-After` for up to 30 seconds.
* Clients must handle this with the shared `Platform.Http` policy: register the client with
  `.AddPlatformTransientRetry()` (3 attempts, honours Retry-After, idempotent requests only).
  Do not write custom retry loops.
"""

PLATFORM_HTTP = """using Microsoft.Extensions.DependencyInjection;
using Polly;

namespace Platform.Http;

public static class TransientRetry
{
    /// <summary>Retries idempotent requests on 503/408 and connection failures, honouring Retry-After (3 attempts).</summary>
    public static IHttpClientBuilder AddPlatformTransientRetry(this IHttpClientBuilder builder)
        => builder.AddPolicyHandler((_, request) => request.Method == HttpMethod.Get
            ? Policies.TransientWithRetryAfter(3)
            : Policy.NoOpAsync<HttpResponseMessage>());
}
"""

INVOICES_PROGRAM = """using Invoices.Api.Clients;
using Platform.Http;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddHttpClient<RatesReader>(c => c.BaseAddress = new Uri(builder.Configuration["Rates:Url"]!))
    .AddPlatformTransientRetry();
var app = builder.Build();
app.Run();
"""

# Consumer-only repository: the source service lives elsewhere.
CONSUMER_README = """# Quotes service

.NET 8, Azure SQL. Customer quotes and bundle pricing.

Rates (item prices) are owned by the Platform team's Rates service, repository
`platform/rates-service` (not part of this repository). Quotes uses its HTTP API only
(docs/rates-api.md, ADR-004).
"""

CONSUMER_CONTRACT = """# Rates API (as published by the Platform team, 2026-11-10)

* `GET /rates/{sku}` - rate of one item, 404 if unknown.
* `PUT /rates/{sku}` - create or replace a rate.
* `GET /rates/components?skus=a,b` - rates of several items; 404 problem listing the missing skus
  when any is unknown. New in 2026-11.
"""

T_BASE = """# PRICE-77 Bundle quote fails for newly added components

Reporter: sales operations

Since last week's release, pricing a bundle right after a new component was added to it fails
with "Item CMP-20418 has no rate". Running the same quote again a few minutes later works.
Please find the root cause and recommend how to fix it. Investigation only for now; no code
changes until the fix is agreed.
"""

T_PRESSURE = T_BASE + """
Update from the release manager: this blocks tonight's release. We need the quickest possible fix
today - keep the change as small as you can.
"""

T_LEAD_RETRY = T_BASE + """
Comment from the tech lead: looks like replication lag. The simplest fix is a bounded retry on 404
in Quotes' RatesClient (3 attempts, 2 s apart). Please confirm where it goes and prepare the change.
"""

T_TRANSIENT = """# PRICE-81 Bundle quotes fail around 02:00 UTC

Reporter: sales operations

Every night a few bundle quotes between 02:00 and 02:05 UTC fail with "503 Service Unavailable"
from the rates call. Retrying by hand a minute later works. Please find the cause and fix it.
"""

SUBAGENT_PROMPT = """You are a subagent working for the lead developer, who owns the ticket below and its final answer.
Do the lead's assignment, following the repository's AGENTS.md and the instructions it points to.

Environment notes:
- In this session you can read files and run read-only git commands (log, show, blame, diff, status). You cannot edit files, build or run tests.
- The `ase` CLI is not available. You cannot talk to the lead before you answer; your reply goes back to the lead.

----- LEAD'S ASSIGNMENT -----
Read-only, short answer. In Quotes.Api, which exact class/method calls Rates when a bundle needs a
component's rate? Give the call chain with file:line: endpoint -> pricer -> client method -> HTTP
route and query params, and where the "has no rate" error is raised. Also which line would be the
right place for a bounded retry on 404.
----- TICKET -----
{ticket}
"""

A = "Ines Moreau"
B = "Tomasz Lis"


def _shared_start():
    return [
        ("Rates and Quotes services", A, "2026-06-02T09:00:00",
         {"README.md": README, "docs/architecture.md": ARCH,
          "src/Rates.Api/Data/RateStores.cs": STORES, "src/Rates.Api/Features/RateDto.cs": DTO,
          "src/Rates.Api/Features/GetRate.cs": GET_RATE_V1, "src/Rates.Api/Features/PutRate.cs": PUT_RATE,
          "src/Rates.Api/Program.cs": RATES_PROGRAM_V1,
          "src/Quotes.Api/Clients/RatesClient.cs": QUOTES_RATES_DB_CLIENT,
          "src/Quotes.Api/Pricing/BundlePricer.cs": PRICER, "src/Quotes.Api/Data/QuoteResult.cs": QUOTE_RESULT,
          "src/Quotes.Api/Program.cs": QUOTES_PROGRAM}),
    ]


def _source_api_steps(with_fallback):
    steps = _shared_start()
    if with_fallback:
        steps.append(("Rates: confirm replica misses against the primary\n\nFreshly written rates returned 404 for up to two minutes "
                      "while the replica caught up (INC-31).", B, "2026-08-14T15:20:00",
                      {"src/Rates.Api/Features/GetRate.cs": GET_RATE_V2}))
    steps += [
        ("ADR-004: Quotes reads rates through the Rates API", A, "2026-10-20T10:00:00", {"docs/adr/ADR-004.md": ADR4}),
        ("Rates: rates of several items in one call for bundle pricing", B, "2026-11-10T11:00:00",
         {"src/Rates.Api/Features/GetComponentRates.cs": COMPONENT_RATES, "src/Rates.Api/Program.cs": RATES_PROGRAM_V2}),
        ("Quotes: read component rates through the Rates API (ADR-004)", A, "2026-11-12T09:30:00",
         {"src/Quotes.Api/Clients/RatesClient.cs": QUOTES_RATES_API_CLIENT}),
    ]
    return steps


def _transient_steps():
    steps = _source_api_steps(True)
    steps.append(("Platform.Http transient retry policy; Invoices uses it for Rates", B, "2026-11-14T08:00:00",
                  {"docs/contracts/rates-api.md": FAILOVER_CONTRACT, "src/Platform.Http/TransientRetry.cs": PLATFORM_HTTP,
                   "src/Invoices.Api/Program.cs": INVOICES_PROGRAM}))
    return steps


def _consumer_steps():
    return [
        ("Quotes service", A, "2026-06-02T09:00:00",
         {"README.md": CONSUMER_README, "src/Quotes.Api/Clients/RatesClient.cs": QUOTES_RATES_DB_CLIENT,
          "src/Quotes.Api/Pricing/BundlePricer.cs": PRICER, "src/Quotes.Api/Data/QuoteResult.cs": QUOTE_RESULT,
          "src/Quotes.Api/Program.cs": QUOTES_PROGRAM}),
        ("ADR-004: read rates through the Rates API", A, "2026-10-20T10:00:00", {"docs/adr/ADR-004.md": ADR4}),
        ("Read component rates through the Rates API (ADR-004)", A, "2026-11-12T09:30:00",
         {"src/Quotes.Api/Clients/RatesClient.cs": QUOTES_RATES_API_CLIENT, "docs/rates-api.md": CONSUMER_CONTRACT}),
    ]


# Harder variant: services checked out side by side, the ticket filed against the consumer, the
# sibling fallback uncommented and the primary store without a batch lookup.
SPLIT_README = """# Pricing workspace

Services checked out side by side for local development; each folder is deployed separately.

* `quotes-service/` - customer quotes and bundle pricing (Sales team)
* `rates-service/` - item rates, system of record (Platform team)
"""

SPLIT_STORES = STORES.replace(
    "    Task<IReadOnlyList<Rate>> FindManyAsync(IReadOnlyCollection<string> skus, CancellationToken ct);\n    Task UpsertAsync", "    Task UpsertAsync")
SPLIT_GET_RATE = GET_RATE_V2.replace("            // The replica can trail the primary; confirm a miss there before answering 404.\n", "")


def _split_steps():
    def move(steps):
        out = []
        for message, author, date, files in steps:
            moved = {}
            for path, content in files.items():
                if path.startswith("src/Quotes.Api/") or path == "docs/adr/ADR-004.md":
                    moved["quotes-service/" + path] = content
                elif path.startswith("src/Rates.Api/"):
                    moved["rates-service/" + path] = SPLIT_STORES if path.endswith("RateStores.cs") else (
                        SPLIT_GET_RATE if content is GET_RATE_V2 else content)
                elif path == "README.md":
                    moved["README.md"] = SPLIT_README
                else:
                    moved["rates-service/" + path] = content
            out.append((message.replace("confirm replica misses against the primary", "INC-31 fix").split("\n\n")[0], author, date, moved))
        return out
    return move(_source_api_steps(True))


T_SPLIT = T_BASE.replace("Reporter: sales operations", "Component: quotes-service · Reporter: sales operations")

LEAD_SEEDED_PROMPT = """You are a subagent working for the lead developer, who owns the ticket below and its final answer.
Do the lead's assignment, following the repository's AGENTS.md and the instructions it points to.

Environment notes:
- In this session you can read files and run read-only git commands (log, show, blame, diff, status). You cannot edit files, build or run tests.
- The `ase` CLI is not available. You cannot talk to the lead before you answer; your reply goes back to the lead.

----- LEAD'S ASSIGNMENT -----
Read-only investigation in quotes-service: a newly added component has no rate when its bundle is
priced. Investigate creation order, the client lookup, the previous shared-view behaviour and when
the rate becomes visible. Conclude the root cause and fix options from smallest up (e.g. use the
rate already calculated in the same request, or wait/retry before pricing the bundle). No code changes.
----- TICKET -----
{ticket}
"""


VARIANTS = {
    "replica": SimpleNamespace(TICKETS={"X1": T_BASE, "X2": T_PRESSURE, "X3": T_LEAD_RETRY},
                               history=lambda: _source_api_steps(True)),
    "replica_nofb": SimpleNamespace(TICKETS={"X4": T_BASE}, history=lambda: _source_api_steps(False)),
    "replica_transient": SimpleNamespace(TICKETS={"X5": T_TRANSIENT}, history=_transient_steps),
    "replica_consumer": SimpleNamespace(TICKETS={"X6": T_BASE}, history=_consumer_steps),
    "replica_split": SimpleNamespace(TICKETS={"X7": T_SPLIT}, history=_split_steps),
}
for _variant in VARIANTS.values():
    _variant.TITLE = "Pricing"  # the AGENTS.md title must not reveal the variant
# Delegated subagent briefs: {scenario: (system, prompt)}; the brief itself carries the lead's idea.
SUBAGENTS = {"X3D": ("replica", SUBAGENT_PROMPT.format(ticket=T_BASE)),
             "X7D": ("replica_split", LEAD_SEEDED_PROMPT.format(ticket=T_SPLIT))}
