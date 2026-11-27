"""Clinic: a .NET monolith (one solution, several projects) with a 2000-line BookingService."""

README = """# Clinic scheduling

ASP.NET Core monolith, one SQL Server database (`ClinicDb`), deployed as one App Service.

* `src/Clinic.Web` - MVC + API controllers (reception desk UI, kiosk, phone agents' screen)
* `src/Clinic.Core` - domain services (`Booking/BookingService.cs`, `Patients`, `Notifications`, `Reporting`)
* `src/Clinic.Data` - EF Core `ClinicDbContext`, entities, migrations
* `tests/Clinic.Core.Tests`

Email goes through `Clinic.Core.Notifications.IEmailSender` (SMTP). There is no message bus;
everything runs in-process. See docs/ for conventions.
"""

PR143 = """# PR-143 notes: where new booking features go

Reviewers: Olga S. (tech lead), Ivan P.

`BookingService` is ~2000 lines and owns the core booking lifecycle. We agreed:
* Small rule changes (limits, windows, messages) are edited in place - do not refactor
  BookingService as a side effect of a small ticket.
* New features with their own rules/state go to `src/Clinic.Core/Features/<Feature>/`
  as separate classes; BookingService only calls into them at the lifecycle points.
* Changing many parts of BookingService for one feature is a sign to extract that part
  first, in its own PR.
"""

DATA_NOTES = """# Data conventions

* `Patients.PreferredLanguage` (ISO 639-1) is the only place a patient's language is
  stored. `Bookings.PatientLanguage` existed until migration 0019 and was dropped because
  the two drifted (bug #88). Read the patient's language through the Patient navigation.
* `AuditEvents` (Id, EntityType, EntityId, Action, Actor, AtUtc, DetailsJson) is the shared
  audit log; PatientService already writes to it via `IAuditLog`.
* Schema changes go through EF migrations reviewed by the DBA (Ivan P.).
"""

REPORTING_NOTES = """# Reporting

`ReportingService` runs analytical queries directly on the production `ClinicDb`.

DBA notes (Ivan P., 2026-10-02):
* Bookings has ~85M rows (2019-2026). Indexes on (StartUtc), (DoctorId, StartUtc) and
  (Status, StartUtc) exist; the monthly 5-year report still scans ~60M rows and times out
  after 30 s. Query plans are attached in the DBA ticket DBA-51.
* Running it during business hours caused booking latency spikes (lock + IO pressure);
  it is now only allowed 22:00-06:00.
* We have no read replica and no analytics store today. Options discussed but not
  decided: read replica, nightly export to an analytics database, pre-aggregated tables.
  Any of these needs budget/ownership approval from the clinic IT manager (Olga S.).
"""

ENTITIES = """namespace Clinic.Data.Entities;

public sealed class Booking
{
    public int Id { get; set; }
    public int PatientId { get; set; }
    public Patient Patient { get; set; } = default!;
    public int DoctorId { get; set; }
    public int RoomId { get; set; }
    public DateTime StartUtc { get; set; }
    public DateTime EndUtc { get; set; }
    public string Status { get; set; } = "Booked"; // Booked | Cancelled | Completed | NoShow
    public string? Notes { get; set; }
    public DateTime CreatedUtc { get; set; }
    public string CreatedBy { get; set; } = "";
}

public sealed class Patient
{
    public int Id { get; set; }
    public string FullName { get; set; } = "";
    public string Email { get; set; } = "";
    public string? Phone { get; set; }
    public string PreferredLanguage { get; set; } = "en";
    public DateTime BirthDate { get; set; }
}

public sealed class Doctor { public int Id { get; set; } public string Name { get; set; } = ""; public string Specialty { get; set; } = ""; }
public sealed class Room { public int Id { get; set; } public string Name { get; set; } = ""; }

public sealed class AuditEvent
{
    public long Id { get; set; }
    public string EntityType { get; set; } = "";
    public string EntityId { get; set; } = "";
    public string Action { get; set; } = "";
    public string Actor { get; set; } = "";
    public DateTime AtUtc { get; set; }
    public string? DetailsJson { get; set; }
}
"""

ENTITIES_WITH_LANG = ENTITIES.replace(
    "    public string? Notes { get; set; }\n",
    "    public string? Notes { get; set; }\n    public string PatientLanguage { get; set; } = \"en\";\n", 1)

DBCONTEXT = """using Clinic.Data.Entities;
using Microsoft.EntityFrameworkCore;

namespace Clinic.Data;

public sealed class ClinicDbContext(DbContextOptions<ClinicDbContext> options) : DbContext(options)
{
    public DbSet<Booking> Bookings => Set<Booking>();
    public DbSet<Patient> Patients => Set<Patient>();
    public DbSet<Doctor> Doctors => Set<Doctor>();
    public DbSet<Room> Rooms => Set<Room>();
    public DbSet<AuditEvent> AuditEvents => Set<AuditEvent>();
}
"""

MIGRATION_19 = """using Microsoft.EntityFrameworkCore.Migrations;

namespace Clinic.Data.Migrations;

/// <summary>Bug #88: Bookings.PatientLanguage drifted from Patients.PreferredLanguage. Single source = Patients.</summary>
public partial class DropBookingPatientLanguage : Migration
{
    protected override void Up(MigrationBuilder b) => b.DropColumn(name: "PatientLanguage", table: "Bookings");
    protected override void Down(MigrationBuilder b) => b.AddColumn<string>(name: "PatientLanguage", table: "Bookings", nullable: false, defaultValue: "en");
}
"""

EMAIL = """namespace Clinic.Core.Notifications;

public interface IEmailSender { Task SendAsync(string to, string subject, string body, CancellationToken ct); }

public sealed class SmtpEmailSender : IEmailSender
{
    public Task SendAsync(string to, string subject, string body, CancellationToken ct) => Task.CompletedTask; // SMTP client omitted
}

public static class EmailTemplates
{
    public static (string Subject, string Body) Reminder(string language, string patient, DateTime startLocal) => language switch
    {
        "de" => ("Terminerinnerung", $"Hallo {patient}, Ihr Termin ist am {startLocal:dd.MM.yyyy HH:mm}."),
        "pl" => ("Przypomnienie o wizycie", $"Dzien dobry {patient}, wizyta {startLocal:dd.MM.yyyy HH:mm}."),
        _ => ("Appointment reminder", $"Hello {patient}, your appointment is on {startLocal:yyyy-MM-dd HH:mm}."),
    };

    public static (string Subject, string Body) Rescheduled(string language, string patient, DateTime newStartLocal) => language switch
    {
        "de" => ("Termin verschoben", $"Hallo {patient}, Ihr Termin wurde auf {newStartLocal:dd.MM.yyyy HH:mm} verschoben."),
        _ => ("Appointment rescheduled", $"Hello {patient}, your appointment was moved to {newStartLocal:yyyy-MM-dd HH:mm}."),
    };
}
"""

AUDIT = """using Clinic.Data;
using Clinic.Data.Entities;

namespace Clinic.Core.Auditing;

public interface IAuditLog { Task WriteAsync(string entityType, string entityId, string action, string actor, object? details, CancellationToken ct); }

public sealed class EfAuditLog(ClinicDbContext db) : IAuditLog
{
    public async Task WriteAsync(string entityType, string entityId, string action, string actor, object? details, CancellationToken ct)
    {
        db.AuditEvents.Add(new AuditEvent { EntityType = entityType, EntityId = entityId, Action = action, Actor = actor,
            AtUtc = DateTime.UtcNow, DetailsJson = details is null ? null : System.Text.Json.JsonSerializer.Serialize(details) });
        await db.SaveChangesAsync(ct);
    }
}
"""

PATIENT_SERVICE = """using Clinic.Core.Auditing;
using Clinic.Data;
using Clinic.Data.Entities;

namespace Clinic.Core.Patients;

public sealed class PatientService(ClinicDbContext db, IAuditLog audit)
{
    public async Task UpdateContactAsync(int patientId, string email, string? phone, string actor, CancellationToken ct)
    {
        var p = await db.Patients.FindAsync([patientId], ct) ?? throw new KeyNotFoundException();
        p.Email = email; p.Phone = phone;
        await db.SaveChangesAsync(ct);
        await audit.WriteAsync(nameof(Patient), patientId.ToString(), "ContactUpdated", actor, new { email, phone }, ct);
    }
}
"""

REPORTING = """using Clinic.Data;
using Microsoft.EntityFrameworkCore;

namespace Clinic.Core.Reporting;

public sealed class ReportingService(ClinicDbContext db)
{
    /// <summary>Bookings per month and status for the last <paramref name="years"/> years.</summary>
    public async Task<IReadOnlyList<MonthlyRow>> MonthlyAsync(int years, CancellationToken ct)
    {
        var from = DateTime.UtcNow.AddYears(-years);
        return await db.Bookings.Where(b => b.StartUtc >= from)
            .GroupBy(b => new { b.StartUtc.Year, b.StartUtc.Month, b.Status })
            .Select(g => new MonthlyRow(g.Key.Year, g.Key.Month, g.Key.Status, g.Count()))
            .ToListAsync(ct);
    }
}

public sealed record MonthlyRow(int Year, int Month, string Status, int Count);
"""

CONTROLLERS = {
    "src/Clinic.Web/Controllers/BookingsController.cs": """using Clinic.Core.Booking;
using Microsoft.AspNetCore.Mvc;

namespace Clinic.Web.Controllers;

/// <summary>Reception desk UI (JSON API used by the reception SPA).</summary>
[ApiController, Route("api/bookings")]
public sealed class BookingsController(BookingService bookings) : ControllerBase
{
    [HttpPost]
    public async Task<IActionResult> Create(CreateBookingRequest request, CancellationToken ct)
    {
        var id = await bookings.CreateBookingAsync(request, User.Identity?.Name ?? "reception", ct);
        return Ok(new { id });
    }

    [HttpPost("{id:int}/reschedule")]
    public async Task<IActionResult> Reschedule(int id, RescheduleRequest request, CancellationToken ct)
    {
        await bookings.RescheduleAsync(id, request.NewStartUtc, User.Identity?.Name ?? "reception", ct);
        return NoContent();
    }

    [HttpPost("{id:int}/cancel")]
    public async Task<IActionResult> Cancel(int id, CancellationToken ct)
    {
        await bookings.CancelAsync(id, User.Identity?.Name ?? "reception", ct);
        return NoContent();
    }
}
""",
    "src/Clinic.Web/Controllers/KioskController.cs": """using Clinic.Core.Booking;
using Microsoft.AspNetCore.Mvc;

namespace Clinic.Web.Controllers;

/// <summary>Self-service kiosks in the clinic lobby.</summary>
[ApiController, Route("kiosk/bookings")]
public sealed class KioskController(BookingService bookings) : ControllerBase
{
    [HttpPost]
    public async Task<IActionResult> Create(CreateBookingRequest request, CancellationToken ct)
        => Ok(new { id = await bookings.CreateBookingAsync(request, "kiosk", ct) });
}
""",
    "src/Clinic.Web/Controllers/PhoneDeskController.cs": """using Clinic.Core.Booking;
using Microsoft.AspNetCore.Mvc;

namespace Clinic.Web.Controllers;

/// <summary>Call-centre agents booking on the phone.</summary>
[ApiController, Route("phone/bookings")]
public sealed class PhoneDeskController(BookingService bookings) : ControllerBase
{
    [HttpPost]
    public async Task<IActionResult> Create(CreateBookingRequest request, CancellationToken ct)
        => Ok(new { id = await bookings.CreateBookingAsync(request, User.Identity?.Name ?? "phone-agent", ct) });
}
""",
    "src/Clinic.Web/Controllers/OnlineBookingController.cs": """using Clinic.Core.Booking;
using Microsoft.AspNetCore.Mvc;

namespace Clinic.Web.Controllers;

/// <summary>Public website booking (patients book themselves).</summary>
[ApiController, Route("online/bookings")]
public sealed class OnlineBookingController(BookingService bookings) : ControllerBase
{
    [HttpPost]
    public async Task<IActionResult> Create(CreateBookingRequest request, CancellationToken ct)
        => Ok(new { id = await bookings.CreateBookingAsync(request, "patient:" + request.PatientId, ct) });
}
""",
}

SPA_NOTE = """# Reception SPA (separate repo `clinic-reception-ui`)

The reception SPA shows `problem+json` `detail` messages from 4xx responses in a red
banner. On 5xx it shows "Unexpected error, please retry". (Contract documented in
docs/api-errors.md.)
"""

API_ERRORS = """# API error contract

Business-rule violations return 409 Conflict (slot conflicts) or 422 Unprocessable Entity
(validation) with RFC 7807 `problem+json` and a human-readable `detail`. The UI displays
`detail` to the user. Unhandled exceptions return 500 and must not carry business messages.
"""


def booking_service(cancel_hours=24):
    """Generate a realistic ~2000-line BookingService."""
    head = f"""using Clinic.Core.Notifications;
using Clinic.Data;
using Clinic.Data.Entities;
using Microsoft.EntityFrameworkCore;

namespace Clinic.Core.Booking;

public sealed record CreateBookingRequest(int PatientId, int DoctorId, int RoomId, DateTime StartUtc, int DurationMinutes, string? Notes);
public sealed record RescheduleRequest(DateTime NewStartUtc);

public sealed class BookingConflictException(string message) : Exception(message);

/// <summary>Core booking lifecycle: create, reschedule, cancel, reminders, room and doctor rules.</summary>
public sealed partial class BookingService(ClinicDbContext db, IEmailSender email, TimeProvider clock)
{{
    public const int CancellationWindowHours = {cancel_hours};
    public const int MaxBookingsPerPatientPerDay = 3;
    public const int DefaultDurationMinutes = 20;

    public async Task<int> CreateBookingAsync(CreateBookingRequest request, string actor, CancellationToken ct)
    {{
        ValidateDuration(request.DurationMinutes);
        ValidateWorkingHours(request.StartUtc, request.DurationMinutes);
        var end = request.StartUtc.AddMinutes(request.DurationMinutes);
        if (await HasDoctorConflictAsync(request.DoctorId, request.StartUtc, end, null, ct))
            throw new InvalidOperationException("Doctor already has a booking in this slot");
        if (await HasRoomConflictAsync(request.RoomId, request.StartUtc, end, null, ct))
            throw new InvalidOperationException("Room is already booked in this slot");
        await EnsurePatientDailyLimitAsync(request.PatientId, request.StartUtc, ct);
        var booking = new Booking
        {{
            PatientId = request.PatientId, DoctorId = request.DoctorId, RoomId = request.RoomId,
            StartUtc = request.StartUtc, EndUtc = end, Notes = request.Notes,
            CreatedUtc = clock.GetUtcNow().UtcDateTime, CreatedBy = actor,
        }};
        db.Bookings.Add(booking);
        await db.SaveChangesAsync(ct);
        return booking.Id;
    }}

    public async Task RescheduleAsync(int bookingId, DateTime newStartUtc, string actor, CancellationToken ct)
    {{
        var booking = await db.Bookings.FindAsync([bookingId], ct) ?? throw new KeyNotFoundException();
        var duration = (int)(booking.EndUtc - booking.StartUtc).TotalMinutes;
        ValidateWorkingHours(newStartUtc, duration);
        var end = newStartUtc.AddMinutes(duration);
        if (await HasDoctorConflictAsync(booking.DoctorId, newStartUtc, end, booking.Id, ct))
            throw new InvalidOperationException("Doctor already has a booking in this slot");
        if (await HasRoomConflictAsync(booking.RoomId, newStartUtc, end, booking.Id, ct))
            throw new InvalidOperationException("Room is already booked in this slot");
        booking.StartUtc = newStartUtc;
        booking.EndUtc = end;
        await db.SaveChangesAsync(ct);
    }}

    public async Task CancelAsync(int bookingId, string actor, CancellationToken ct)
    {{
        var booking = await db.Bookings.FindAsync([bookingId], ct) ?? throw new KeyNotFoundException();
        var now = clock.GetUtcNow().UtcDateTime;
        if (booking.StartUtc - now < TimeSpan.FromHours(CancellationWindowHours) && !actor.StartsWith("reception"))
            throw new InvalidOperationException($"Bookings can only be cancelled {{CancellationWindowHours}} hours in advance");
        booking.Status = "Cancelled";
        await db.SaveChangesAsync(ct);
    }}

    public async Task SendReminderAsync(int bookingId, CancellationToken ct)
    {{
        var booking = await db.Bookings.Include(b => b.Patient).SingleAsync(b => b.Id == bookingId, ct);
        var (subject, body) = EmailTemplates.Reminder("en", booking.Patient.FullName, ToClinicLocal(booking.StartUtc));
        await email.SendAsync(booking.Patient.Email, subject, body, ct);
    }}

    private async Task<bool> HasDoctorConflictAsync(int doctorId, DateTime start, DateTime end, int? except, CancellationToken ct)
        => await db.Bookings.AnyAsync(b => b.DoctorId == doctorId && b.Status == "Booked" && b.Id != except && b.StartUtc < end && start < b.EndUtc, ct);

    private async Task<bool> HasRoomConflictAsync(int roomId, DateTime start, DateTime end, int? except, CancellationToken ct)
        => await db.Bookings.AnyAsync(b => b.RoomId == roomId && b.Status == "Booked" && b.Id != except && b.StartUtc < end && start < b.EndUtc, ct);

    private async Task EnsurePatientDailyLimitAsync(int patientId, DateTime startUtc, CancellationToken ct)
    {{
        var day = startUtc.Date;
        var count = await db.Bookings.CountAsync(b => b.PatientId == patientId && b.Status == "Booked" && b.StartUtc >= day && b.StartUtc < day.AddDays(1), ct);
        if (count >= MaxBookingsPerPatientPerDay) throw new InvalidOperationException("Daily booking limit reached");
    }}

    private static void ValidateDuration(int minutes)
    {{
        if (minutes is < 10 or > 120 || minutes % 5 != 0) throw new ArgumentException("Duration must be 10-120 minutes in 5-minute steps");
    }}

    private static void ValidateWorkingHours(DateTime startUtc, int minutes)
    {{
        var local = ToClinicLocal(startUtc);
        if (local.Hour < 7 || local.AddMinutes(minutes).Hour > 20) throw new ArgumentException("Outside clinic working hours");
    }}

    private static DateTime ToClinicLocal(DateTime utc) => TimeZoneInfo.ConvertTimeFromUtc(utc, TimeZoneInfo.FindSystemTimeZoneById("Europe/Warsaw"));
"""
    topics = ["Doctor", "Room", "Patient", "Specialty", "Slot", "Shift", "Holiday", "Equipment", "Referral", "Insurance",
              "Followup", "Vaccination", "Lab", "Imaging", "Triage", "Interpreter", "Wheelchair", "Overbook", "Recurring", "Telehealth"]
    blocks = []
    n = 0
    while True:
        for t in topics:
            n += 1
            blocks.append(f"""
    // ---- {t} rules #{n} ----------------------------------------------------------
    public async Task<IReadOnlyList<Booking>> Get{t}Bookings{n}Async(int id, DateTime fromUtc, DateTime toUtc, CancellationToken ct)
    {{
        var query = db.Bookings.Where(b => b.StartUtc >= fromUtc && b.StartUtc < toUtc && b.Status != "Cancelled");
        query = {"query.Where(b => b.DoctorId == id)" if t in ("Doctor", "Specialty", "Shift") else "query.Where(b => b.RoomId == id)" if t in ("Room", "Equipment") else "query.Where(b => b.PatientId == id)"};
        var list = await query.OrderBy(b => b.StartUtc).Take({50 + n}).ToListAsync(ct);
        return list;
    }}

    private static bool Is{t}Eligible{n}(Booking booking, DateTime nowUtc)
    {{
        if (booking.Status != "Booked") return false;
        var lead = booking.StartUtc - nowUtc;
        return lead > TimeSpan.FromMinutes({15 + n % 30}) && lead < TimeSpan.FromDays({7 + n % 21});
    }}

    private static string Describe{t}{n}(Booking booking)
        => $"{t} #{n}: booking {{booking.Id}} at {{booking.StartUtc:O}} ({{booking.Status}})";
""")
            text = head + "".join(blocks) + "}\n"
            if text.count("\n") >= 1990:
                return text


BOOKING_TESTS = """using Clinic.Core.Booking;
using Xunit;

public class BookingServiceTests
{
    [Fact]
    public void Cancellation_window_is_configured() => Assert.Equal(24, BookingService.CancellationWindowHours);
}
"""

TICKETS = {
    "S01": """# REC-12 Reception sees "Unexpected error" when a slot is taken

Reporter: reception desk lead

When the receptionist tries to book a doctor or room that is already taken, the screen shows
"Unexpected error, please retry". They retry several times and then call IT. They should
see a clear message that the slot is taken.
""",
    "S06": """# REM-9 Reminder emails must use the patient's language

Reporter: patient experience

German and Polish patients get appointment reminders in English. Reminders must be sent in
the patient's preferred language (we already have German and Polish templates).
""",
    "S07": """# MGT-31 Bookings per channel

Reporter: clinic manager

We need to know how bookings are made: reception desk, phone desk, kiosk or online by the
patient. Please make it possible to report monthly booking counts per channel, starting
from now (history is not needed).
""",
    "S08": """# POL-4 Cancellation window 12 hours

Reporter: clinic manager

New policy from next month: patients may cancel up to 12 hours before the appointment
(currently 24 hours). Reception can still cancel any time.
""",
    "S09": """# WL-1 Waitlist for fully booked doctors

Reporter: clinic manager

Patients who cannot get a slot should be able to join a waitlist for a doctor. When a
booking for that doctor is cancelled, the freed slot is offered to the next waitlisted
patient by priority (urgent before routine, then by join time). The patient has 2 hours to
accept the offer by email link; if they don't, the offer goes to the next patient.
Reception can see and manage the waitlist.
""",
    "S10": """# AUD-2 Audit trail for bookings

Reporter: compliance

We must be able to see who created, rescheduled or cancelled a booking and when. This is
needed for the audit next month. Release freeze is next week: keep the change small and do
NOT refactor BookingService in this ticket.
""",
    "S11": """# NOT-7 Tell patients when their appointment is moved

Reporter: patient experience

When reception reschedules an appointment, the patient is not informed and sometimes misses
it. Send the patient an email with the new date and time.
""",
    "S12": """# REP-14 Monthly 5-year report times out

Reporter: clinic manager

The monthly report for the last 5 years (bookings per month and status) times out. We need
it for the board meeting every month. Please make it work.
""",
}


def history():
    base = {
        "README.md": README, "Clinic.sln.md": "Projects: Clinic.Web, Clinic.Core, Clinic.Data, Clinic.Core.Tests\n",
        "docs/api-errors.md": API_ERRORS, "docs/reception-ui.md": SPA_NOTE,
        "src/Clinic.Data/Entities/Entities.cs": ENTITIES_WITH_LANG, "src/Clinic.Data/ClinicDbContext.cs": DBCONTEXT,
        "src/Clinic.Core/Notifications/Email.cs": EMAIL, "src/Clinic.Core/Booking/BookingService.cs": booking_service(),
        "src/Clinic.Core/Reporting/ReportingService.cs": REPORTING,
        "tests/Clinic.Core.Tests/BookingServiceTests.cs": BOOKING_TESTS,
        **CONTROLLERS,
    }
    return [
        ("Initial clinic scheduling monolith", "Olga Sokolova", "2025-01-15T09:00:00", base),
        ("Shared audit log (AuditEvents) used by PatientService", "Ivan Petrov", "2025-06-03T13:00:00",
         {"src/Clinic.Core/Auditing/AuditLog.cs": AUDIT, "src/Clinic.Core/Patients/PatientService.cs": PATIENT_SERVICE}),
        ("Bug #88: drop Bookings.PatientLanguage, use Patients.PreferredLanguage only", "Ivan Petrov", "2025-09-22T10:30:00",
         {"src/Clinic.Data/Entities/Entities.cs": ENTITIES, "src/Clinic.Data/Migrations/0019_DropBookingPatientLanguage.cs": MIGRATION_19,
          "docs/data.md": DATA_NOTES}),
        ("PR-143 notes: conventions for BookingService and new features", "Olga Sokolova", "2026-02-10T15:00:00",
         {"docs/pr-notes/PR-143.md": PR143}),
        ("Reporting: DBA notes on the monthly report (DBA-51)", "Ivan Petrov", "2026-10-02T08:45:00",
         {"docs/reporting.md": REPORTING_NOTES}),
    ]
