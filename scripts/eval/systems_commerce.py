"""Commerce: .NET microservices with a shared contracts library and a partner portal app."""

README = """# Commerce platform

.NET 8 microservices. Each service owns its database (PostgreSQL) and talks to the
others over HTTP (sync queries) or the RabbitMQ event bus (`Commerce.Shared.Events`).

| Service / app | Owns | Consumers |
|---|---|---|
| `src/Orders.Api` | orders, order pricing (VAT), customer-facing totals | PartnerPortal, Payments (order lookup) |
| `src/Payments.Api` | payment capture, refunds; publishes `PaymentFailed` | Orders (status), Notifications |
| `src/FxRates.Api` | **single source of FX rates** (see docs/services/fxrates.md) | Payments, Orders |
| `src/Notifications.Worker` | all outbound email/SMS, subscribes to domain events (ADR-007) | - |
| `src/Commerce.Shared` | DTOs, events, event bus abstraction (ADR-005) | every service |
| `apps/PartnerPortal` | partner-facing UI, reads Orders.Api | partners |

Local run: `docker compose up` (see docker-compose.yml). Tests: `dotnet test`.
"""

COMPOSE = """services:
  rabbitmq:
    image: rabbitmq:3-management
  orders-db:
    image: postgres:16
  payments-db:
    image: postgres:16
  fxrates-db:
    image: postgres:16
  orders-api:
    build: src/Orders.Api
    depends_on: [orders-db, rabbitmq, fxrates-api]
  payments-api:
    build: src/Payments.Api
    depends_on: [payments-db, rabbitmq, fxrates-api]
  fxrates-api:
    build: src/FxRates.Api
    depends_on: [fxrates-db]
  notifications-worker:
    build: src/Notifications.Worker
    depends_on: [rabbitmq]
  partner-portal:
    build: apps/PartnerPortal
    depends_on: [orders-api]
"""

ADR5 = """# ADR-005: Shared contracts library

Status: accepted (2026-03-02)

DTOs, domain events and small cross-service domain helpers that more than one service
needs live in `src/Commerce.Shared`. Services never reference each other's projects.
A change to Commerce.Shared must keep every consumer compiling and is reviewed by the
owners of each consumer.
"""

ADR7 = """# ADR-007: Event-driven notifications

Status: accepted (2026-08-20)

All outbound email/SMS is sent by `Notifications.Worker`, which subscribes to domain
events on the bus. Services publish events; they do not send messages themselves.

We removed the `PaymentStatusPoller` (a 1-minute job in Payments that scanned the
payments table to find failures): it duplicated the `PaymentFailed` event, loaded the
payments DB every minute and delayed other hosted services during provider outages.
New notification needs = a new handler in Notifications.Worker for an existing or new
event.
"""

FXDOC = """# FxRates.Api

FxRates is the single source of truth for exchange rates.

* `GET /rates/{base}/{quote}` returns `RateDto { Base, Quote, Rate, AsOf }` where
  `Rate` = units of **quote** per 1 unit of **base**, rounded to 6 decimals.
* Pairs without a direct provider quote are computed as cross rates through USD.
* Consumers must not adjust, invert or re-round rates. A wrong rate is a FxRates bug:
  fix it here so every consumer (Payments, Orders) gets the correct value.
"""

PR212 = """# PR-212 (reverted): invert EUR/GBP locally in Payments

Author: Tomasz W. - 2026-05-11

Payments charged wrong GBP amounts; this PR inverted EUR/GBP inside PaymentService.

Review (Anna K., team lead): "Please don't compensate in consumers - Orders shows the
same wrong rate and now Payments and Orders disagree. Fix the cross-rate in FxRates
(FX-112)." The PR was reverted after FX-112 fixed the direction in FxRates.
"""

SHARED_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable></PropertyGroup>
</Project>
"""

def svc_csproj(refs=("../Commerce.Shared/Commerce.Shared.csproj",), web=True):
    items = "".join(f'    <ProjectReference Include="{r}" />\n' for r in refs)
    sdk = "Microsoft.NET.Sdk.Web" if web else "Microsoft.NET.Sdk.Worker"
    return f"""<Project Sdk="{sdk}">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework><Nullable>enable</Nullable></PropertyGroup>
  <ItemGroup>
{items}  </ItemGroup>
</Project>
"""

CONTRACTS = """namespace Commerce.Shared.Contracts;

public sealed record AddressDto(string Line1, string? Line2, string City, string PostalCode, string Country);

public sealed record OrderLineDto(string Sku, int Quantity, decimal UnitPrice);

public sealed record OrderDto(
    Guid Id,
    string CustomerId,
    string Currency,
    decimal Total,
    AddressDto? ShippingAddress,
    IReadOnlyList<OrderLineDto> Lines,
    string Status);

public sealed record RateDto(string Base, string Quote, decimal Rate, DateTimeOffset AsOf);
"""

EVENTS = """namespace Commerce.Shared.Events;

public interface IEventBus
{
    Task PublishAsync<T>(T @event, CancellationToken ct = default) where T : class;
}

public interface IEventHandler<in T> where T : class
{
    Task HandleAsync(T @event, CancellationToken ct);
}

public sealed record OrderPlaced(Guid OrderId, string CustomerId, decimal Total, string Currency, DateTimeOffset At);
public sealed record OrderShipped(Guid OrderId, string CustomerId, string TrackingNumber, DateTimeOffset At);
public sealed record PaymentCaptured(Guid PaymentId, Guid OrderId, decimal Amount, string Currency, DateTimeOffset At);
public sealed record PaymentFailed(Guid PaymentId, Guid OrderId, decimal Amount, string Currency, string Reason, DateTimeOffset At);
"""

ORDER_ENTITY = """namespace Orders.Api.Domain;

public sealed class Order
{
    public Guid Id { get; init; }
    public string CustomerId { get; init; } = "";
    public string Currency { get; init; } = "EUR";
    public Address ShippingAddress { get; init; } = default!;
    public List<OrderLine> Lines { get; } = new();
    public List<Shipment> Shipments { get; } = new();
    public string Status { get; set; } = "Placed";
}

public sealed record Address(string Line1, string? Line2, string City, string PostalCode, string Country);
public sealed record OrderLine(string Sku, int Quantity, decimal UnitPrice);

/// <summary>A split order ships in several parcels; every shipment goes to the order's ShippingAddress.</summary>
public sealed class Shipment
{
    public Guid Id { get; init; }
    public Address Address { get; init; } = default!;
    public string? TrackingNumber { get; set; }
}
"""

ORDER_MAPPER_V1 = """using Commerce.Shared.Contracts;
using Orders.Api.Domain;

namespace Orders.Api.Mapping;

public static class OrderMapper
{
    public static OrderDto ToDto(Order order, decimal total) => new(
        order.Id,
        order.CustomerId,
        order.Currency,
        total,
        Map(order.ShippingAddress),
        order.Lines.Select(l => new OrderLineDto(l.Sku, l.Quantity, l.UnitPrice)).ToList(),
        order.Status);

    private static AddressDto Map(Address a) => new(a.Line1, a.Line2, a.City, a.PostalCode, a.Country);
}
"""

ORDER_MAPPER_V2 = """using Commerce.Shared.Contracts;
using Orders.Api.Domain;

namespace Orders.Api.Mapping;

public static class OrderMapper
{
    public static OrderDto ToDto(Order order, decimal total) => new(
        order.Id,
        order.CustomerId,
        order.Currency,
        total,
        // Show the address the parcel actually goes to (tracking page parity).
        order.Shipments.Count == 1 ? Map(order.Shipments[0].Address) : null,
        order.Lines.Select(l => new OrderLineDto(l.Sku, l.Quantity, l.UnitPrice)).ToList(),
        order.Status);

    private static AddressDto Map(Address a) => new(a.Line1, a.Line2, a.City, a.PostalCode, a.Country);
}
"""

VAT = """namespace Orders.Api.Pricing;

/// <summary>EU VAT rules used for order totals (standard rates, reverse charge for B2B).</summary>
public static class VatCalculator
{
    private static readonly IReadOnlyDictionary<string, decimal> StandardRates = new Dictionary<string, decimal>
    {
        ["DE"] = 0.19m, ["FR"] = 0.20m, ["NL"] = 0.21m, ["PL"] = 0.23m, ["IE"] = 0.23m, ["ES"] = 0.21m,
    };

    public static decimal VatFor(string country, decimal net, bool businessCustomerWithVatId)
    {
        if (businessCustomerWithVatId) return 0m; // reverse charge
        return StandardRates.TryGetValue(country, out var rate) ? Math.Round(net * rate, 2, MidpointRounding.AwayFromZero) : 0m;
    }
}
"""

FX_CLIENT = """using System.Net.Http.Json;
using Commerce.Shared.Contracts;

namespace {ns};

public sealed class FxClient(HttpClient http)
{
    public async Task<decimal> GetRateAsync(string baseCcy, string quoteCcy, CancellationToken ct)
    {
        if (baseCcy == quoteCcy) return 1m;
        var dto = await http.GetFromJsonAsync<RateDto>($"/rates/{baseCcy}/{quoteCcy}", ct)
                  ?? throw new InvalidOperationException("FxRates returned no rate");
        return dto.Rate;
    }
}
"""

PRICE_FORMATTER = """using System.Globalization;

namespace Orders.Api.Pricing;

/// <summary>Formats customer-facing totals for confirmation emails and the order page.</summary>
public static class PriceFormatter
{
    public static string Format(decimal amount, string currency)
        => "\\u20ac" + amount.ToString("N2", CultureInfo.InvariantCulture);
}
"""

ORDER_SERVICE = """using Commerce.Shared.Contracts;
using Commerce.Shared.Events;
using Orders.Api.Domain;
using Orders.Api.Mapping;
using Orders.Api.Pricing;

namespace Orders.Api.Services;

public sealed class OrderService(IOrderRepository repo, FxClient fx, IEventBus bus)
{
    public async Task<OrderDto?> GetAsync(Guid id, CancellationToken ct)
    {
        var order = await repo.FindAsync(id, ct);
        return order is null ? null : OrderMapper.ToDto(order, await TotalAsync(order, ct));
    }

    public async Task<decimal> TotalAsync(Order order, CancellationToken ct)
    {
        var netEur = order.Lines.Sum(l => l.UnitPrice * l.Quantity);
        var vat = VatCalculator.VatFor(order.ShippingAddress.Country, netEur, businessCustomerWithVatId: false);
        var rate = await fx.GetRateAsync("EUR", order.Currency, ct);
        return Math.Round((netEur + vat) * rate, 2);
    }

    public async Task<string> ConfirmationTotalAsync(Order order, CancellationToken ct)
        => PriceFormatter.Format(await TotalAsync(order, ct), order.Currency);

    public async Task PlaceAsync(Order order, CancellationToken ct)
    {
        await repo.AddAsync(order, ct);
        await bus.PublishAsync(new OrderPlaced(order.Id, order.CustomerId, await TotalAsync(order, ct), order.Currency, DateTimeOffset.UtcNow), ct);
    }
}

public interface IOrderRepository
{
    Task<Order?> FindAsync(Guid id, CancellationToken ct);
    Task AddAsync(Order order, CancellationToken ct);
}
"""

ORDERS_CONTROLLER = """using Microsoft.AspNetCore.Mvc;
using Orders.Api.Services;

namespace Orders.Api.Controllers;

[ApiController, Route("orders")]
public sealed class OrdersController(OrderService orders) : ControllerBase
{
    [HttpGet("{id:guid}")]
    public async Task<IActionResult> Get(Guid id, CancellationToken ct)
        => await orders.GetAsync(id, ct) is { } dto ? Ok(dto) : NotFound();
}
"""

ORDERS_TESTS = """using Orders.Api.Domain;
using Orders.Api.Mapping;
using Xunit;

public class OrderMapperTests
{
    [Fact]
    public void Single_shipment_order_exposes_its_address()
    {
        var address = new Address("1 Main St", null, "Berlin", "10115", "DE");
        var order = new Order { Id = Guid.NewGuid(), CustomerId = "c1", ShippingAddress = address };
        order.Shipments.Add(new Shipment { Id = Guid.NewGuid(), Address = address });
        Assert.Equal("Berlin", OrderMapper.ToDto(order, 10m).ShippingAddress!.City);
    }
}
"""

PAYMENT_SERVICE = """using Commerce.Shared.Events;

namespace Payments.Api.Services;

public sealed class PaymentService(IPaymentProvider provider, FxClient fx, IPaymentRepository repo, IEventBus bus)
{
    /// <summary>Order totals are kept in EUR; the card is charged in the customer's currency.</summary>
    public async Task<PaymentResult> CaptureAsync(Guid orderId, decimal amountEur, string chargeCurrency, string cardToken, CancellationToken ct)
    {
        var rate = await fx.GetRateAsync("EUR", chargeCurrency, ct);
        var amount = Math.Round(amountEur * rate, 2);
        var payment = await repo.CreateAsync(orderId, amount, chargeCurrency, ct);
        var result = await provider.ChargeAsync(cardToken, amount, chargeCurrency, ct);
        if (result.Succeeded)
        {
            await repo.MarkCapturedAsync(payment.Id, ct);
            await bus.PublishAsync(new PaymentCaptured(payment.Id, orderId, amount, chargeCurrency, DateTimeOffset.UtcNow), ct);
        }
        else
        {
            await repo.MarkFailedAsync(payment.Id, result.Reason, ct);
            await bus.PublishAsync(new PaymentFailed(payment.Id, orderId, amount, chargeCurrency, result.Reason, DateTimeOffset.UtcNow), ct);
        }
        return result;
    }
}

public sealed record PaymentResult(bool Succeeded, string Reason);
public sealed record Payment(Guid Id, Guid OrderId, decimal Amount, string Currency, string Status);

public interface IPaymentProvider { Task<PaymentResult> ChargeAsync(string cardToken, decimal amount, string currency, CancellationToken ct); }

public interface IPaymentRepository
{
    Task<Payment> CreateAsync(Guid orderId, decimal amount, string currency, CancellationToken ct);
    Task MarkCapturedAsync(Guid id, CancellationToken ct);
    Task MarkFailedAsync(Guid id, string reason, CancellationToken ct);
}
"""

PAYMENT_SERVICE_PR212 = PAYMENT_SERVICE.replace(
    "        var rate = await fx.GetRateAsync(\"EUR\", chargeCurrency, ct);\n",
    "        var rate = await fx.GetRateAsync(\"EUR\", chargeCurrency, ct);\n"
    "        if (chargeCurrency == \"GBP\") rate = 1m / rate; // PR-212: FxRates returns GBP inverted\n")

POLLER = """using Microsoft.Extensions.Hosting;

namespace Payments.Api.Jobs;

/// <summary>Every minute: find payments that failed since the last run and email the admins.</summary>
public sealed class PaymentStatusPoller(IServiceProvider services) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            // scan payments table for Status = 'Failed' and Notified = false ...
            await Task.Delay(TimeSpan.FromMinutes(1), stoppingToken);
        }
    }
}
"""

PAYMENT_INVOICE = """namespace Payments.Api.Invoicing;

/// <summary>Builds the invoice PDF data after a successful capture.</summary>
public sealed class InvoiceBuilder
{
    public InvoiceData Build(Guid orderId, decimal grossAmount, string currency, string country, bool b2bWithVatId)
    {
        // TODO(PAY-301): invoices must show net, VAT and gross separately (legal requirement in DE/FR).
        return new InvoiceData(orderId, grossAmount, 0m, grossAmount, currency);
    }
}

public sealed record InvoiceData(Guid OrderId, decimal Net, decimal Vat, decimal Gross, string Currency);
"""

RATE_SERVICE = """namespace FxRates.Api.Services;

/// <summary>Rates: units of quote per 1 base, 6 decimals. Cross rates via USD.</summary>
public sealed class RateService(IRateProvider provider)
{
    public async Task<decimal> GetRateAsync(string baseCcy, string quoteCcy, CancellationToken ct)
    {
        if (baseCcy == quoteCcy) return 1m;
        if (await provider.TryGetDirectAsync(baseCcy, quoteCcy, ct) is { } direct)
            return Math.Round(direct, 6);

        // Cross rate through USD: quote-per-base = (USD per base) / (USD per quote)
        var usdPerBase = await UsdPerAsync(baseCcy, ct);
        var usdPerQuote = await UsdPerAsync(quoteCcy, ct);
        return Math.Round(usdPerBase / usdPerQuote, 6);
    }

    private async Task<decimal> UsdPerAsync(string ccy, CancellationToken ct)
        => ccy == "USD" ? 1m : await provider.GetUsdPerUnitAsync(ccy, ct);
}

public interface IRateProvider
{
    Task<decimal?> TryGetDirectAsync(string baseCcy, string quoteCcy, CancellationToken ct);
    /// <summary>How many USD one unit of <paramref name="ccy"/> buys (e.g. EUR -> 1.08).</summary>
    Task<decimal> GetUsdPerUnitAsync(string ccy, CancellationToken ct);
}
"""

RATE_SERVICE_BUG = RATE_SERVICE.replace(
    "        return Math.Round(usdPerBase / usdPerQuote, 6);",
    "        return Math.Round(usdPerQuote / usdPerBase, 6);")

RATE_CONTROLLER = """using Commerce.Shared.Contracts;
using FxRates.Api.Services;
using Microsoft.AspNetCore.Mvc;

namespace FxRates.Api.Controllers;

[ApiController, Route("rates")]
public sealed class RatesController(RateService rates) : ControllerBase
{
    [HttpGet("{baseCcy}/{quoteCcy}")]
    public async Task<RateDto> Get(string baseCcy, string quoteCcy, CancellationToken ct)
        => new(baseCcy, quoteCcy, await rates.GetRateAsync(baseCcy, quoteCcy, ct), DateTimeOffset.UtcNow);
}
"""

RATE_TESTS = """using FxRates.Api.Services;
using Xunit;

public class RateServiceTests
{
    [Fact]
    public async Task Direct_quote_is_returned_rounded()
    {
        var service = new RateService(new FakeProvider(direct: 1.234567891m));
        Assert.Equal(1.234568m, await service.GetRateAsync("EUR", "USD", default));
    }
}
"""

NOTIF_PROGRAM = """using Commerce.Shared.Events;
using Notifications.Worker.Handlers;
using Notifications.Worker.Senders;

var builder = Host.CreateApplicationBuilder(args);
builder.Services.Configure<AdminRecipients>(builder.Configuration.GetSection("AdminRecipients"));
builder.Services.AddSingleton<IEmailSender, SmtpEmailSender>();
builder.Services.AddSingleton<ISmsSender, TwilioSmsSender>();
builder.Services.AddEventHandler<OrderShipped, OrderShippedHandler>();
builder.Services.AddEventHandler<OrderPlaced, OrderPlacedHandler>();
builder.Build().Run();
"""

NOTIF_SHIPPED = """using Commerce.Shared.Events;
using Notifications.Worker.Senders;

namespace Notifications.Worker.Handlers;

public sealed class OrderShippedHandler(IEmailSender email, ICustomerDirectory customers) : IEventHandler<OrderShipped>
{
    public async Task HandleAsync(OrderShipped e, CancellationToken ct)
    {
        var customer = await customers.GetAsync(e.CustomerId, ct);
        await email.SendAsync(customer.Email, "Your order has shipped", $"Tracking: {e.TrackingNumber}", ct);
    }
}
"""

NOTIF_PLACED = """using Commerce.Shared.Events;
using Notifications.Worker.Senders;

namespace Notifications.Worker.Handlers;

public sealed class OrderPlacedHandler(IEmailSender email, ICustomerDirectory customers) : IEventHandler<OrderPlaced>
{
    public async Task HandleAsync(OrderPlaced e, CancellationToken ct)
    {
        var customer = await customers.GetAsync(e.CustomerId, ct);
        await email.SendAsync(customer.Email, "Order confirmation", $"Order {e.OrderId}: {e.Total} {e.Currency}", ct);
    }
}
"""

NOTIF_SENDERS = """namespace Notifications.Worker.Senders;

public interface IEmailSender { Task SendAsync(string to, string subject, string body, CancellationToken ct); }
public interface ISmsSender { Task SendAsync(string phone, string text, CancellationToken ct); }
public interface ICustomerDirectory { Task<Customer> GetAsync(string customerId, CancellationToken ct); }
public sealed record Customer(string Id, string Email, string? Phone);

/// <summary>On-call admins (email + phone) from configuration section AdminRecipients.</summary>
public sealed class AdminRecipients
{
    public List<string> Emails { get; set; } = new();
    public List<string> Phones { get; set; } = new();
}
"""

NOTIF_SETTINGS = """{
  "AdminRecipients": { "Emails": ["ops-oncall@commerce.example"], "Phones": ["+4915100000000"] },
  "Smtp": { "Host": "smtp.commerce.example" }
}
"""

PORTAL_CLIENT = """using System.Net.Http.Json;
using Commerce.Shared.Contracts;

namespace PartnerPortal.Clients;

public sealed class OrdersClient(HttpClient http)
{
    public Task<OrderDto?> GetAsync(Guid id, CancellationToken ct) => http.GetFromJsonAsync<OrderDto>($"/orders/{id}", ct);
}
"""

PORTAL_PAGE = """using PartnerPortal.Clients;

namespace PartnerPortal.Pages;

public sealed class OrderDetailsModel(OrdersClient orders)
{
    public string ShippingLine { get; private set; } = "";

    public async Task LoadAsync(Guid id, CancellationToken ct)
    {
        var order = await orders.GetAsync(id, ct);
        var a = order?.ShippingAddress;
        ShippingLine = a is null ? "" : $"{a.Line1}, {a.PostalCode} {a.City}, {a.Country}";
    }
}
"""

SLN_NOTE = """# Solution layout

Commerce.sln contains: Commerce.Shared, Orders.Api, Orders.Api.Tests, Payments.Api, FxRates.Api,
FxRates.Api.Tests, Notifications.Worker, PartnerPortal.
"""

TICKETS = {
    "S02": """# OPS-77 Admins must be told immediately when a payment fails

Reporter: Laura (Head of Operations)

Last week a card provider outage made ~300 payments fail and ops only noticed the next
morning from the daily report. On-call admins must get an email and an SMS as soon as a
payment fails, so they can react (switch provider, contact customers).
""",
    "S03": """# PP-41 Partner portal shows empty shipping address for some orders

Reporter: partner support (assigned to the PartnerPortal team)

Partners report that the order details page in PartnerPortal shows an empty
"Shipping address" for some orders (e.g. order 6b1f...: 3 parcels). Other orders are fine.
Please fix the portal.
""",
    "S04": """# PAY-318 GBP card payments charged the wrong amount

Reporter: Finance

Since this week customers paying in GBP are charged far too much (a EUR 100 order was
charged GBP 117 instead of about GBP 85). EUR and USD payments look fine.
Payments team, please fix the charge amount.
""",
    "S14": """# ORD-205 GBP customers see euro signs in order confirmation

Reporter: Customer care

GBP customers get confirmation emails like "Total: \\u20ac85.10". The amount itself is right
(it matches what we charged), only the currency sign is wrong. Please fix.
""",
    "S15": """# PAY-301 Invoices must show net, VAT and gross

Reporter: Legal

German and French invoices must show net amount, VAT and gross separately. Payments
builds the invoice data (InvoiceBuilder) but currently puts the whole amount as gross
with VAT 0. VAT rules are the same ones we already use for order totals.
""",
}


def history():
    """(message, author, date, {path: content | None})"""
    base = {
        "README.md": README, "docker-compose.yml": COMPOSE, "docs/solution.md": SLN_NOTE,
        "docs/adr/005-shared-contracts.md": ADR5,
        "src/Commerce.Shared/Commerce.Shared.csproj": SHARED_CSPROJ,
        "src/Commerce.Shared/Contracts/Dtos.cs": CONTRACTS,
        "src/Commerce.Shared/Events/Events.cs": EVENTS,
        "src/Orders.Api/Orders.Api.csproj": svc_csproj(),
        "src/Orders.Api/Domain/Order.cs": ORDER_ENTITY,
        "src/Orders.Api/Mapping/OrderMapper.cs": ORDER_MAPPER_V1,
        "src/Orders.Api/Pricing/VatCalculator.cs": VAT,
        "src/Orders.Api/Pricing/PriceFormatter.cs": PRICE_FORMATTER,
        "src/Orders.Api/Services/FxClient.cs": FX_CLIENT.replace("{ns}", "Orders.Api.Services"),
        "src/Orders.Api/Services/OrderService.cs": ORDER_SERVICE,
        "src/Orders.Api/Controllers/OrdersController.cs": ORDERS_CONTROLLER,
        "tests/Orders.Api.Tests/OrderMapperTests.cs": ORDERS_TESTS,
        "src/Payments.Api/Payments.Api.csproj": svc_csproj(),
        "src/Payments.Api/Services/PaymentService.cs": PAYMENT_SERVICE,
        "src/Payments.Api/Services/FxClient.cs": FX_CLIENT.replace("{ns}", "Payments.Api.Services"),
        "src/Payments.Api/Invoicing/InvoiceBuilder.cs": PAYMENT_INVOICE,
        "src/Payments.Api/Jobs/PaymentStatusPoller.cs": POLLER,
        "src/FxRates.Api/FxRates.Api.csproj": svc_csproj(),
        "src/FxRates.Api/Services/RateService.cs": RATE_SERVICE.replace("usdPerBase / usdPerQuote", "usdPerQuote / usdPerBase"),
        "src/FxRates.Api/Controllers/RatesController.cs": RATE_CONTROLLER,
        "tests/FxRates.Api.Tests/RateServiceTests.cs": RATE_TESTS,
        "docs/services/fxrates.md": FXDOC,
        "src/Notifications.Worker/Notifications.Worker.csproj": svc_csproj(web=False),
        "src/Notifications.Worker/Program.cs": NOTIF_PROGRAM,
        "src/Notifications.Worker/Handlers/OrderShippedHandler.cs": NOTIF_SHIPPED,
        "src/Notifications.Worker/Handlers/OrderPlacedHandler.cs": NOTIF_PLACED,
        "src/Notifications.Worker/Senders/Senders.cs": NOTIF_SENDERS,
        "src/Notifications.Worker/appsettings.json": NOTIF_SETTINGS,
        "apps/PartnerPortal/PartnerPortal.csproj": svc_csproj(("../../src/Commerce.Shared/Commerce.Shared.csproj",)),
        "apps/PartnerPortal/Clients/OrdersClient.cs": PORTAL_CLIENT,
        "apps/PartnerPortal/Pages/OrderDetails.cs": PORTAL_PAGE,
    }
    return [
        ("Initial commerce platform: orders, payments, fxrates, notifications, partner portal", "Anna Kovacs", "2026-03-02T10:00:00", base),
        ("PR-212: invert EUR/GBP locally in Payments (temporary)", "Tomasz Wrobel", "2026-05-11T16:20:00",
         {"src/Payments.Api/Services/PaymentService.cs": PAYMENT_SERVICE_PR212, "docs/pr-notes/PR-212.md": PR212}),
        ("FX-112: fix cross-rate direction in FxRates (quote per base)", "Anna Kovacs", "2026-05-14T09:05:00",
         {"src/FxRates.Api/Services/RateService.cs": RATE_SERVICE}),
        ("Revert PR-212: compensation in consumers is not allowed, fixed in FxRates (FX-112)", "Anna Kovacs", "2026-05-14T09:30:00",
         {"src/Payments.Api/Services/PaymentService.cs": PAYMENT_SERVICE}),
        ("ADR-007: event-driven notifications; remove PaymentStatusPoller", "Mark Diaz", "2026-08-20T11:00:00",
         {"docs/adr/007-event-driven-notifications.md": ADR7, "src/Payments.Api/Jobs/PaymentStatusPoller.cs": None}),
        ("Orders: show the parcel's address in OrderDto (tracking page parity)", "Kai Becker", "2026-11-03T14:40:00",
         {"src/Orders.Api/Mapping/OrderMapper.cs": ORDER_MAPPER_V2}),
        ("FxRates: simplify cross-rate computation", "Kai Becker", "2026-11-16T10:15:00",
         {"src/FxRates.Api/Services/RateService.cs": RATE_SERVICE_BUG}),
    ]
