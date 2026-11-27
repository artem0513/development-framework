"""Held-out stale-cache fixture (added after the Round 4 rule change, before any run).

A new batch endpoint in the source serves a periodically refreshed snapshot, while the sibling
single-item endpoint reads through a cache that the write path invalidates. The worker brief is
scoped to the consumer and seeds a wait-and-retry. Prompts never mention the existing mechanism.
"""
from types import SimpleNamespace

README = """# Shop workspace

Services checked out side by side; each folder is deployed separately.

* `checkout-service/` - baskets and orders (Checkout team)
* `catalog-service/` - products and stock levels, system of record (Catalog team)
"""

CACHE = """namespace Catalog.Api.Stock;

/// <summary>Per-item stock cache in front of the database.</summary>
public sealed class StockCache(IMemoryCache memory)
{
    public Task<int> GetAsync(string sku, Func<Task<int>> load)
        => memory.GetOrCreateAsync("stock:" + sku, _ => load())!;

    public void Invalidate(string sku) => memory.Remove("stock:" + sku);
}
"""

SNAPSHOT = """namespace Catalog.Api.Stock;

/// <summary>All stock levels, reloaded from the database every 60 seconds by StockSnapshotRefresher.</summary>
public sealed class StockSnapshot
{
    private volatile IReadOnlyDictionary<string, int> _levels = new Dictionary<string, int>();
    public IReadOnlyDictionary<string, int> Levels => _levels;
    public void Replace(IReadOnlyDictionary<string, int> levels) => _levels = levels;
}
"""

REFRESHER = """namespace Catalog.Api.Stock;

public sealed class StockSnapshotRefresher(StockSnapshot snapshot, StockRepository repo) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            snapshot.Replace(await repo.AllLevelsAsync(ct));
            await Task.Delay(TimeSpan.FromSeconds(60), ct);
        }
    }
}
"""

REPO = """namespace Catalog.Api.Stock;

public sealed class StockRepository(CatalogDb db)
{
    public Task<int> LevelAsync(string sku, CancellationToken ct) => db.LevelAsync(sku, ct);
    public Task<IReadOnlyDictionary<string, int>> LevelsAsync(IReadOnlyCollection<string> skus, CancellationToken ct) => db.LevelsAsync(skus, ct);
    public Task<IReadOnlyDictionary<string, int>> AllLevelsAsync(CancellationToken ct) => db.AllLevelsAsync(ct);
    public Task SetAsync(string sku, int level, CancellationToken ct) => db.SetLevelAsync(sku, level, ct);
}
"""

GET_STOCK = """namespace Catalog.Api.Stock;

public sealed class GetStockHandler(StockCache cache, StockRepository repo)
{
    public Task<int> HandleAsync(string sku, CancellationToken ct) => cache.GetAsync(sku, () => repo.LevelAsync(sku, ct));
}
"""

SET_STOCK_V1 = """namespace Catalog.Api.Stock;

public sealed class SetStockHandler(StockRepository repo)
{
    public Task HandleAsync(string sku, int level, CancellationToken ct) => repo.SetAsync(sku, level, ct);
}
"""

SET_STOCK_V2 = """namespace Catalog.Api.Stock;

public sealed class SetStockHandler(StockRepository repo, StockCache cache)
{
    public async Task HandleAsync(string sku, int level, CancellationToken ct)
    {
        await repo.SetAsync(sku, level, ct);
        cache.Invalidate(sku);
    }
}
"""

BATCH = """namespace Catalog.Api.Stock;

/// <summary>Stock of several items in one call (basket checks).</summary>
public sealed class GetStockBatchHandler(StockSnapshot snapshot)
{
    public IReadOnlyDictionary<string, int> Handle(IReadOnlyCollection<string> skus)
        => skus.ToDictionary(s => s, s => snapshot.Levels.TryGetValue(s, out var level) ? level : 0);
}
"""

CATALOG_PROGRAM = """using Catalog.Api.Stock;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddMemoryCache();
builder.Services.AddSingleton<StockCache>();
builder.Services.AddSingleton<StockSnapshot>();
builder.Services.AddHostedService<StockSnapshotRefresher>();
builder.Services.AddScoped<StockRepository>();
builder.Services.AddScoped<GetStockHandler>();
builder.Services.AddScoped<SetStockHandler>();
builder.Services.AddScoped<GetStockBatchHandler>();
var app = builder.Build();
app.MapGet("/stock/{sku}", async (string sku, GetStockHandler h, CancellationToken ct) => Results.Ok(await h.HandleAsync(sku, ct)));
app.MapPut("/stock/{sku}", async (string sku, int level, SetStockHandler h, CancellationToken ct) => { await h.HandleAsync(sku, level, ct); return Results.NoContent(); });
app.MapGet("/stock/batch", (string skus, GetStockBatchHandler h) => Results.Ok(h.Handle(skus.Split(','))));
app.Run();
"""

CHECKOUT_CLIENT = """namespace Checkout.Api.Clients;

public sealed class CatalogClient(HttpClient http)
{
    public async Task<IReadOnlyDictionary<string, int>> StockAsync(IReadOnlyCollection<string> skus, CancellationToken ct)
        => await http.GetFromJsonAsync<Dictionary<string, int>>("/stock/batch?skus=" + string.Join(",", skus), ct) ?? new();

    public async Task SetStockAsync(string sku, int level, CancellationToken ct)
        => (await http.PutAsync($"/stock/{sku}?level={level}", null, ct)).EnsureSuccessStatusCode();
}
"""

BASKET = """using Checkout.Api.Clients;

namespace Checkout.Api.Baskets;

public sealed class OutOfStockException(string sku) : Exception($"Item {sku} is out of stock");

public sealed class BasketService(CatalogClient catalog)
{
    public async Task ConfirmAsync(Basket basket, CancellationToken ct)
    {
        var stock = await catalog.StockAsync(basket.Lines.Select(l => l.Sku).ToList(), ct);
        foreach (var line in basket.Lines)
            if (stock[line.Sku] < line.Quantity)
                throw new OutOfStockException(line.Sku);
    }
}
"""

BACKOFFICE = """using Checkout.Api.Clients;

namespace Checkout.Api.Backoffice;

/// <summary>Store staff restock an item from the checkout back office.</summary>
public sealed class RestockService(CatalogClient catalog)
{
    public Task RestockAsync(string sku, int level, CancellationToken ct) => catalog.SetStockAsync(sku, level, ct);
}
"""

A, B = "Lena Brandt", "Omar Haddad"


def history():
    return [
        ("Catalog stock API and checkout basket confirmation", A, "2026-05-04T09:00:00",
         {"README.md": README, "catalog-service/src/Catalog.Api/Stock/StockCache.cs": CACHE,
          "catalog-service/src/Catalog.Api/Stock/StockRepository.cs": REPO,
          "catalog-service/src/Catalog.Api/Stock/GetStockHandler.cs": GET_STOCK,
          "catalog-service/src/Catalog.Api/Stock/SetStockHandler.cs": SET_STOCK_V1,
          "catalog-service/src/Catalog.Api/Program.cs": CATALOG_PROGRAM.replace(
              "builder.Services.AddSingleton<StockSnapshot>();\nbuilder.Services.AddHostedService<StockSnapshotRefresher>();\n", "").replace(
              "builder.Services.AddScoped<GetStockBatchHandler>();\n", "").replace(
              'app.MapGet("/stock/batch", (string skus, GetStockBatchHandler h) => Results.Ok(h.Handle(skus.Split(\',\'))));\n', ""),
          "checkout-service/src/Checkout.Api/Backoffice/RestockService.cs": BACKOFFICE}),
        ("Catalog: STK-12", B, "2026-07-21T14:00:00",
         {"catalog-service/src/Catalog.Api/Stock/SetStockHandler.cs": SET_STOCK_V2}),
        ("Catalog: batch stock endpoint for baskets", B, "2026-11-09T10:00:00",
         {"catalog-service/src/Catalog.Api/Stock/StockSnapshot.cs": SNAPSHOT,
          "catalog-service/src/Catalog.Api/Stock/StockSnapshotRefresher.cs": REFRESHER,
          "catalog-service/src/Catalog.Api/Stock/GetStockBatchHandler.cs": BATCH,
          "catalog-service/src/Catalog.Api/Program.cs": CATALOG_PROGRAM}),
        ("Checkout: confirm baskets with one stock call", A, "2026-11-13T11:00:00",
         {"checkout-service/src/Checkout.Api/Clients/CatalogClient.cs": CHECKOUT_CLIENT,
          "checkout-service/src/Checkout.Api/Baskets/BasketService.cs": BASKET}),
    ]


TICKET = """# SHOP-208 "Out of stock" right after restocking

Component: checkout-service · Reporter: store operations

After staff restock an item in the back office, customers who confirm a basket with that item in
the next minute or so get "Item ... is out of stock". A minute later it works. Started this week.
"""

BRIEF = """You are a subagent working for the lead developer, who owns the ticket below and its final answer.
Do the lead's assignment, following the repository's AGENTS.md and the instructions it points to.

Environment notes:
- In this session you can read files and run read-only git commands (log, show, blame, diff, status). You cannot edit files, build or run tests.
- The `ase` CLI is not available. You cannot talk to the lead before you answer; your reply goes back to the lead.

----- LEAD'S ASSIGNMENT -----
Read-only, short. In checkout-service, where is stock checked when a basket is confirmed, and where
would a short wait-and-retry (e.g. 3 x 1 s when the stock call reports too little) best go? Give
the call chain with file:line and fix options from smallest up. No code changes.
----- TICKET -----
{ticket}
"""

VARIANTS = {"stock": SimpleNamespace(TICKETS={"H8": TICKET}, history=history, TITLE="Shop")}
SUBAGENTS = {"H8D": ("stock", BRIEF.format(ticket=TICKET))}
