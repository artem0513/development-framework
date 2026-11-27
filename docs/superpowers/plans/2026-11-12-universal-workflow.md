# Universal DevelopmentFramework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Этот блок относится к работе исполнителя над планом в доступной среде. Он не вводит зависимость поставляемого DevelopmentFramework от Superpowers. Метод выполнения выбирается в пределах разрешений пользователя; пользователь разрешил реализацию и независимые параллельные подзадачи.

**Goal:** переделать DevelopmentFramework в универсальный процесс без MCP для локальных Codex, Claude Code и Copilot, сохранив строгую software-проверку и добавив актуализируемую память, согласование и журнал работы.

**Architecture:** канонические скиллы управляют поведением, локальный Python CLI хранит состояние и проверяет формальные условия. SQLite изолирована по проектам; общий registry задаёт стабильную идентичность. Отдельные CLI adapters возвращают runner-captured receipts, а domain profiles определяют доказательства результата.

**Tech Stack:** Python 3.11+, standard library, SQLite, Git для software, локальные CLI агентов, JSON, Markdown; проверенные IANA TZif-данные для Europe/Bucharest без Python-зависимости tzdata.

**Spec:** [Дизайн универсального процесса](../specs/2026-11-12-universal-workflow-design.md).

**Status (12 ноября 2026):** задачи 0–12 реализованы и проверены в согласованных локальных границах: по 430 тестов прошли на Windows/Python 3.12 и 3.15, новая живая смешанная задача Codex завершена, пакет проверен на обеих версиях. История этапов и два найденных P2 каталогов исправлены. Последнее независимое адресное дополнение — 6/6 PASS, отдельное ревью без замечаний; граница относительно предыдущей приёмки и ограничения сохранены. Недоступные live backend/VM/remote CI, реальная установка/миграция и ручная очистка тестового инцидента не выдаются за выполненные проверки. [Итог реализации](../../implementation-report-2026-11-12.md), [валидация](../../universal-workflow-validation.md), [журнал](../../worklog/2026-11-12.md), [контекст проекта](../../../outputs/universal-workflow/RESUME.md). Чеклисты ниже сохранены как исходная программа проверок; фактическое выполнение и исключения приведены в отчёте. Исходный HEAD: `a294111a3d953f999564a6354165c81c9f3cd90c`.

**Уточнение исполнения 12 ноября:** пользователь разрешил реализацию и затем исключил установку/живое тестирование Claude Code и Copilot из текущей приёмки, так как подписок нет. Во всех задачах ниже три живых backend заменяются живым Codex плюс протокольными тестами Claude/Copilot и качественной оценкой риска. Отсутствие живых результатов этих двух backend явно остаётся ограничением, но не блокирует согласованную поставку.

## Global Constraints

- Runtime: Python 3.11+ и стандартная библиотека; `dependencies = []` сохраняется.
- Один компьютер; память проектов изолирована; автоматической синхронизации и слияния копий нет.
- Собственный MCP-сервер, постоянный фоновый сервис и обязательная зависимость от большого агентного SDK отсутствуют.
- Schema v4 мигрирует в v5 через backup и транзакцию; registry получает собственную schema v1.
- Task contract v2; v1 сохраняется как `legacy_v1`, без автоматического нового согласования.
- `max_stage_cycles=3`, `max_task_cycles=12`, `repeat_failure_limit=2`, `max_diagnostic_passes=1`, `diagnostic_timeout_seconds=1200`, `active_time_budget_seconds=null`.
- Один ведущий workflow; существенная реализация начинается только после явного согласования `proposal_hash`.
- `Europe/Bucharest` — timezone проекта по умолчанию; UTC — формат хранения времени; rules DST не заменяются постоянным offset.
- P0/P1/P2 блокируют software approval; P3 advisory. Независимые AC/review, positive test counts, отсутствие skipped required tests и актуальность receipts сохраняются.
- Не менять глобальный routing, auth, sandbox, чужие hooks/MCP и пользовательские настройки как побочный эффект установки.
- Документирование, установка, миграция существующей установки и внешняя публикация — разные действия; разрешение на этот план не разрешает последние три.
- Любой code/config milestone требует `python -m unittest discover -s tests -v` и независимых AC/review по актуальному кандидату. Протокольные fixtures не являются живой семантической проверкой.
- Коммиты и публикация выполняются только в рамках фактической пользовательской авторизации; инструкции о частых commits не предоставляют её сами.

## Review Focus

1. Совпадение хэшей известных файлов при изменившейся неучтённой зависимости: полнота не предполагается; discovery должен уметь расширить область и отметить неизвестное. Тесты задачи 4.
2. Смена агента/root во время незавершённой задачи: сохраняются UUID, попытки, история и ограничения согласования; прежний cwd не подменяется новым. Тесты задач 1, 2, 8.
3. Потеря ответа после создания ресурса: UNKNOWN, сверка фактического состояния и отсутствие дублирующего эффекта. Тест задачи 8.
4. Finish при BLOCKED/ожидании ответа и два параллельных завершения: сессия закрывается атомарно, задача не становится COMPLETED. Тесты задачи 9.
5. Чужие CLI hooks/config и отличающиеся permissions: инструкции read-only недостаточно; ограничения и чистый старт проверяются отрицательными canary. Тесты задач 5, 6, 10.

## Последовательность и правила выполнения

Задачи идут в указанном порядке. После задачи 0 нужно подтвердить транспорт на доступной реальной среде до расширения runtime. Задачи 1–2 создают основу, 3–4 — исследование и память, 5–8 — выполнение, 9 — журнал, 10–11 — поставку и перенос, 12 — общую приёмку.

Для каждой задачи: сначала добавить указанный поведенческий тест, выполнить его с ожидаемым содержательным FAIL, внести минимальную реализацию, повторить targeted checks и полный suite. Отсутствующий модуль на первом запуске — ожидаемый red, но перед заявлением регрессии тест должен проверять описанное поведение. После изменения общего протокола повторяются относящиеся проверки ранее завершённых задач.

На milestone сохраняются diff, реальные outputs и независимые AC/review; исправления запускают свежие проверки. Промежуточные протокольные этапы не объявляются полной поддержкой Claude/Copilot до живой приёмки задачи 12. Обнаруженный blocker не разрешает понижать требования, подменять модель fixtures или сбрасывать лимиты.

## Task 0: Зафиксировать baseline и доказать возможность транспорта

**Files:** read `ase/core.py`, `ase/host.py`, `scripts/install.py`, `tests/test_host.py`, `docs/ADR-002.md`; create future `docs/cli-compatibility.md`; results в разрешённой временной области, затем ссылки в compatibility doc без credentials.

**Interfaces:** consumes текущие CLI и пользовательские разрешения; produces фактическая матрица backend/version/auth method/read-only controls/structured output/child launch и перечень старых host safeguards для переноса.

- [ ] Запустить полный suite и сохранить фактический baseline. Последние 179 PASS из предыдущего ревью не использовать как сегодняшний результат.
- [ ] Проверить `--version`/`--help`, путь executable, актуальную документацию и доступность каждого CLI. Отсутствие Claude/Copilot в исходном PATH оформить как setup blocker; не устанавливать их и не менять auth без разрешения.
- [ ] В одноразовой области выполнить две отдельные read-only сессии доступного backend над простым известным дефектом. Проверить завершение, формат payload, отсутствие product/config mutation и отдельные session/process identities.
- [ ] Проверить отрицательные сценарии: запрет записи reviewer, отсутствие hooks/MCP startup, отказ child startup в restricted sandbox. Отсутствие API key не исправлять копированием credentials; Claude bare mode не считать совместимым с subscription auth по умолчанию.
- [ ] Сохранить матрицу реальной возможности и source links. При невозможности безопасного CLI-запуска остановить зависимую часть плана с конкретным blocker; не писать транспорт на основе предположения.

**Check:** полный unittest suite, реальные CLI exit/status/артефакты. Успех этапа означает доказанный путь хотя бы для первого backend и явно известные blockers остальных; release acceptance по-прежнему требует все три.

## Task 1: Стабильная идентичность и безопасная схема памяти

**Files:** create `ase/projects.py`, `tests/test_projects.py`, `tests/test_schema_v5.py`; modify `ase/store.py`, `ase/__main__.py`, `tests/test_knowledge.py`, `tests/test_continuity.py`, `tests/test_system.py` только где меняется номер схемы/создание Store.

**Interfaces:** define `ProjectContext` dataclass с `project_id: str`, `workspace_id: str`, `root: Path`, `storage_key: str`, `kind: str`, `state_home: Path`, `timezone: str`. `ProjectRegistry(state_home: Path).register(root: Path, *, kind: str, timezone: str) -> ProjectContext`; `.resolve(selector: str) -> ProjectContext`; `.attach(project_id: str, root: Path, *, expected_revision: int) -> ProjectContext`. `Store` принимает `Path | ProjectContext`, сохраняя вызовы legacy Engine.

- [ ] Добавить `test_rename_keeps_identity_and_history`: после явного attach нового root `project_id` и knowledge IDs прежние; `workspace_id` и source observations не смешиваются с другим worktree.
- [ ] Добавить `test_v4_migration_keeps_artifact_paths_and_is_recoverable`: старый storage key/absolute receipt paths сохранены, schema становится 5, pre-migration backup открывается старым кодом; сбой не оставляет частичной схемы.
- [ ] Добавить `test_registry_recovery_and_isolation`: сбой между PREPARING и project DB восстанавливается идемпотентно; неактивный registry record недоступен для run; другой проект не получает чужие facts.
- [ ] Реализовать registry v1, additive migration v5 и явный attach. Claims из v4 получают UNKNOWN. Для workspace без Git не вызывать Git snapshot; software оставляет прежнюю source fingerprint семантику.
- [ ] Проверить приоритет `--home > ASE_HOME > platform default`, canonical Windows paths, symlink/hardlink guards и конфликт двух registrations.

**Check:** `python -m unittest discover -s tests -p test_projects.py -v` и `... -p test_schema_v5.py -v`; затем полный suite. Никаких миграций реальной пользовательской БД на этом этапе без отдельного разрешения.

## Task 2: Версии задач, предложение и согласование

**Files:** create `ase/tasks.py`, `tests/test_task_contract_v2.py`, `templates/task-v2.json`, `templates/proposal.json`; modify `ase/__main__.py`, `ase/core.py` как compatibility facade, `templates/project-profile.json`.

**Interfaces:** `TaskService(store).create(contract: dict) -> dict`; `.propose(task_id: str, proposal: dict, *, expected_revision: int) -> dict`; `.approve(task_id: str, *, proposal_hash: str, user_message_ref: str, approved_stage_ids: list[str]) -> dict`; `.require_approval(task_id: str, stage_id: str) -> dict`; `.update_execution_plan(task_id: str, plan: dict, *, expected_revision: int) -> dict`; `.transition(task_id: str, target: str, *, expected_revision: int, reason: str) -> dict`. Task snapshots содержат `task_id`, `revision`, `state`, `contract_hash`, `proposal_hash`; execution plan snapshot — собственные revision/plan_hash.

- [ ] Добавить `test_execution_requires_exact_proposal`: create/propose не разрешают run; сохранённое согласие нужного хэша разрешает ровно перечисленные stages; changed material proposal блокирует выполнение.
- [ ] Добавить `test_partial_scope_preserves_parent_obligations`: согласованная обратимая часть выполняема, зависимый неизвестный этап не выполняем, parent не COMPLETED; child наследует только scope согласованного parent stage, без нового интервью или расширения полномочий.
- [ ] Добавить `test_legacy_contract_has_no_new_authority`: v1 история/лимиты доступны, но v2 approval не возникает; stale CAS и повтор чужого message ref не переписывают принятую версию.
- [ ] Реализовать strict v2 validation, states из дизайна, assumptions/unknowns/evidence mapping и approved stage scope. Добавление проверки в execution plan допустимо без нового approval только при неизменных baseline specifications и разрешённых scope/effects; `test_extra_check_does_not_change_approved_goal` доказывает это и отказ при удалении required check. Runtime требует source ref согласия; UI/скилл отвечает за соответствие реальному сообщению, без заявления криптографической достоверности.
- [ ] Добавить CLI `task create/propose/approve/update-plan`; существующие `start/run/resume/check/verify/gate/report` dispatch различают legacy и v2. Прямой поддельный `approved: true` отклоняется.

**Check:** `python -m unittest discover -s tests -p test_task_contract_v2.py -v`, включая сохранение после перезапуска и параллельный approve/update.

## Task 3: Разбор источников и точечный discovery

**Files:** create `ase/discovery.py`, `tests/test_discovery.py`, `templates/intake.json`, `templates/discovery.json`; modify `ase/core.py:discover`, `ase/continuity.py`, `ase/__main__.py`.

**Interfaces:** `discover_capabilities(project: ProjectContext, *, scope: dict, backend_config: dict) -> dict`; `validate_intake(payload: dict) -> dict`; `record_discovery(store, task_id: str, payload: dict, *, expected_revision: int) -> dict`. Intake содержит outcomes, decisions/proposals, contradictions, source_refs и gaps; discovery содержит scoped resources, dependencies, candidate checks, selected tools/skills и limitations.

- [ ] Добавить `test_meeting_suggestion_is_not_decision`: fixture встречи с предложением и неразборчивым решением сохраняет разные статусы и timestamps; обязательный пробел не исчезает из parent contract.
- [ ] Добавить `test_discovery_is_scoped_and_expands_by_reason`: unrelated canary-файл не читается; существенная найденная зависимость включается с причиной; ошибка доступа остаётся unknown.
- [ ] Добавить `test_project_test_conventions_override_heuristics`: pytest из pyproject не превращается в unittest, packageManager/Bun lock учитываются, несколько .NET solutions выдаются кандидатами без молчаливого выбора первого.
- [ ] Реализовать структурные валидаторы и manifest кандидатов. Runtime не обещает сам семантически расшифровать встречу: модель и выбранный доменный инструмент готовят intake, runtime сохраняет происхождение и пробелы.
- [ ] Проверить `discovery record`/`capabilities` CLI: read-only probes не устанавливают tools, не собирают аккаунты и не выполняют найденные repository scripts до их осмотра.

**Check:** `python -m unittest discover -s tests -p test_discovery.py -v`; сценарии Python, Bun, multi-solution и non-Git.

## Task 4: Проверка актуальности знаний и обновление зависимостей

**Files:** modify `ase/knowledge.py`, `ase/continuity.py`, `templates/knowledge.json`; create `tests/test_knowledge_refresh.py`.

**Interfaces:** extend `Knowledge.record(payload: dict, *, expected_revision: int | None = None) -> dict`; add `.retrieve(*, topic: str | None, scope: dict, refresh: bool = True) -> dict` и `.record_observations(observations: list[dict], dependencies: list[dict], *, expected_revision: int) -> dict`. Retrieve возвращает claims, validity, refresh_required и limitations, не переписывает семантические statements автоматически.

- [ ] Добавить `test_changed_dependency_invalidates_only_related_claims`: A зависит от изменённого source; B зависит от A; C не связан. A/B STALE, C CURRENT после собственной проверки источника.
- [ ] Добавить `test_dirty_missing_and_unrecorded_dependencies`: dirty file меняет актуальность без смены HEAD; недоступный source даёт UNAVAILABLE; manifest не заявляет полноту неизвестных dependencies.
- [ ] Добавить `test_claim_history_cas_and_cycles`: одновременно supersede может один writer; dependency cycle не зацикливает refresh; предыдущая версия и причины остаются доступны.
- [ ] Реализовать наблюдения по content hashes и live resource probes, политики обновления и требование pre-mutation observation для infrastructure. Векторную БД/AST index не добавлять.
- [ ] Проверить смену worktree/root: facts с чужим workspace/revision не выдаются как CURRENT, historical user decisions сохраняются с областью применимости.

**Check:** `python -m unittest discover -s tests -p test_knowledge_refresh.py -v` и существующий `test_knowledge.py`.

## Task 5: Общий runner и Codex adapter без MCP

**Files:** create `ase/runner.py`, `ase/protocol.py`, `ase/adapters/__init__.py`, `ase/adapters/codex.py`, `tests/test_runner.py`, `tests/test_codex_adapter.py`, `tests/test_adapter_security.py`; modify `ase/core.py:_codex/_verify/execute`, `ase/inspection.py` только при необходимом переиспользовании safeguards.

**Interfaces:** `BackendAdapter.probe(config: dict) -> dict`; `.build_invocation(request: dict, capabilities: dict) -> dict`; `.parse_result(raw_events: list[dict], final_payload: object) -> dict`. Invocation содержит `argv`, `cwd`, `stdin`, `permission_description`, `artifact_paths`; request содержит role, project, source/contract/proposal/plan/check hashes, schema и handoff. `Runner.run(adapter, request: dict, *, timeout: int) -> dict` возвращает process receipt и normalized result. `validate_payload(payload: object, schema: dict) -> dict` валидирует только явно поддерживаемое подмножество schema; неизвестные обязательные конструкции дают BLOCKED.

- [ ] Добавить `test_receipt_requires_real_subprocess`: runner создаёт exit/times/hash независимо от stdout PASS; forged payload без запуска не создаёт APPROVED-compatible receipt.
- [ ] Добавить `test_two_fresh_roles_and_current_inputs`: implementation/AC/review имеют разные execution IDs; несовпадение request hash, terminal event, schema или output hash блокирует результат.
- [ ] Перенести execute и Codex argv-building без изменения behavior software checks. Убрать зависимость runner от `ase.host` и специального `mcp_servers.autonomous_se` toggle; child startup исключает неразрешённую конфигурацию по подтверждённым flags.
- [ ] Перенести host safeguards в transport-neutral tests: root/state boundaries, inspector read-only allowlist, stdin isolation, Git fsmonitor/textconv/filter/hook canaries, redirects/hardlinks и concurrency. Составить соответствие каждому старому test_host case; ничего не удалять до эквивалентного теста.
- [ ] Временный compatibility bridge читает существующие v1 receipts, не переписывая их. Пробный реальный Codex AC/review в чистой области проходит без MCP; blocked child startup даёт typed blocker без расширения прав.

**Check:** targeted runner/adapter/security suites, существующие system/gate tests, полный suite, отдельная живая Codex квитанция.

## Task 6: Claude Code и Copilot adapters

**Files:** create `ase/adapters/claude.py`, `ase/adapters/copilot.py`, `tests/test_claude_adapter.py`, `tests/test_copilot_adapter.py`; update `docs/cli-compatibility.md`, `ase/adapters/__init__.py`.

**Interfaces:** тот же `BackendAdapter` и нормализованные role schemas из задачи 5. Backend selection explicit в execution policy; automatic provider failover отсутствует. Capabilities включают installed version, auth availability, clean-start controls, permission level и supported schema/events.

- [ ] Добавить `test_claude_missing_structured_output_is_blocked`: exit 0 и JSON envelope без требуемого structured payload не считаются завершённой проверкой; permission_denials сохраняются.
- [ ] Добавить `test_copilot_requires_terminal_result_and_valid_schema`: JSONL с промежуточным сообщением, прерванный stream, неизвестный terminal event и malformed JSON не дают semantic PASS.
- [ ] Добавить `test_backend_config_is_not_target_code`: target hooks/extensions/MCP canaries не запускаются; заявленный запрет записи проверяется в disposable workspace; нет wildcard permission grant или credentials copying.
- [ ] Реализовать adapters по фактическим flags из задачи 0. Read-only policy и OS sandbox отражаются отдельно. Не использовать недокументированное поле events или schema flag только по аналогии с Codex.
- [ ] Выполнить отдельный живой role-scenario для Claude и Copilot после доступного authorized setup. Если auth/CLI недоступны, сохранить BLOCKED и не объявлять задачу поддержки backend завершённой.

**Check:** targeted adapter tests и реальные captured outputs каждого backend. Детерминированные subprocess fixtures отмечены как protocol-only.

## Task 7: Общие evidence profiles и сохранение software gate

**Files:** create `ase/profiles.py`, `ase/gates.py`, `tests/test_profiles.py`, `tests/test_gate_regression.py`, `templates/profiles/software.json`, `templates/profiles/infrastructure.json`, `templates/profiles/document.json`, `templates/profiles/research.json`; modify `ase/core.py:check/gate/report`, `ase/tasks.py`.

**Interfaces:** `validate_evidence_spec(spec: dict, profile: str) -> dict`; `evaluate_gate(contract: dict, current: dict, receipts: list[dict], judgments: list[dict]) -> dict`. Gate возвращает прежние status/exit semantics и раздельные `rejected_reasons`/`blocked_reasons`. `evaluate_profile(profile: str, specs: list[dict], observations: list[dict]) -> dict` не вызывает LLM и не доверяет self-reported status.

- [ ] Добавить `test_software_gate_parity`: ноль/skip/failed tests, mandatory mapping, tamper, stale source/contract, P2, protected changes и high-risk rules дают те же запреты, что исходный gate. Для v2 `test_changed_execution_plan_requires_fresh_receipts` отклоняет прежний plan_hash; legacy v1 использует прежний frozen contract без выдуманного нового receipt.
- [ ] Добавить `test_nonsoftware_requires_actual_postconditions`: отсутствие unittest для VM допустимо только при infrastructure profile; отсутствующий/неуспешный probe блокирует AC; модельный PASS его не перекрывает.
- [ ] Добавить `test_unknown_evaluator_and_mixed_parent_coverage`: unknown evidence kind BLOCKED; parent mixed не APPROVED при неподтверждённом обязательстве дочернего этапа.
- [ ] Извлечь gate с сохранением legacy facade. Реализовать evaluator kinds первой версии: command receipt, versioned artifact/hash, structured probe predicate и явное manual attestation для заранее определённого ручного AC. Точное predicate-подмножество — equality, membership, numeric bound; произвольный eval запрещён.
- [ ] Проверить software golden fixtures на новом gate и заново выполнить deliberate-defect scenario: receipt от старого кандидата и optional failed command, связанный с mandatory AC, не одобряются.

**Check:** `test_profiles.py`, `test_gate_regression.py`, существующие `test_system.py`, `test_protected_authorization.py`, `test_bun_counts.py`, полный suite.

## Task 8: Ограниченный workflow, fallback и операции с эффектом

**Files:** create `ase/workflow.py`, `ase/operations.py`, `tests/test_workflow_v2.py`, `tests/test_operations.py`; modify `ase/tasks.py`, `ase/core.py:run/resume`, `ase/__main__.py`.

**Interfaces:** `WorkflowController(store, runner).run(task_id: str, *, resume: bool = False) -> dict`; `.request_stop(task_id: str, *, reason: str) -> dict`; `OperationLedger(store).prepare(task_id: str, stage_id: str, action_id: str, intent: dict) -> dict`; `.reconcile(action_id: str, observation: dict) -> dict`. Controller вызывает services задач 2, 5, 7; не реализует их правила второй раз.

- [ ] Добавить `test_three_stage_twelve_task_cycles_survive_resume`: число кандидатов ограничено 3/12; retry дочерней задачи или новая contract revision не обнуляют расходы; v1 max_attempts не повышается автоматически.
- [ ] Добавить `test_second_same_cause_uses_one_diagnostic_pass`: второй повтор переводит в DIAGNOSING; запуск заранее записан, timeout 1200; повтор после crash/исчерпания не создаёт второй diagnostic run; изменение условий требует новой approval.
- [ ] Добавить `test_optional_active_time_budget_counts_union`: пересекающиеся интервалы считаются один раз; PAUSED/ожидание пользователя исключены; default null не отключает обязательные cycle/process limits.
- [ ] Добавить `test_lost_create_ack_reconciles_without_duplicate`: fixture внешнего resource API применяет первое действие и теряет ответ. На resume — UNKNOWN и status probe; второй ресурс не создаётся. Rollback не выполняется автоматически.
- [ ] Реализовать execution loop, signature-assisted diagnostics, counters, stop и distinct transient probe retries. Любой unresolved blocked_reason останавливает mutation, даже если общий verdict REJECTED.

**Check:** targeted v2/operations tests, существующие P2/remediation/resume tests, в том числе остановка в середине последней проверки и отсутствие повторной реализации.

## Task 9: Датированный журнал, finish и сводки для митингов

**Files:** create `ase/journal.py`, `ase/timezones.py`, `ase/data/timezones/Europe/Bucharest`, `ase/data/timezones/provenance.json`, `templates/session-finish.json`, `tests/test_journal.py`, `tests/test_timezones.py`; modify `ase/continuity.py`, `ase/__main__.py`, `pyproject.toml` для включения TZif как data, не зависимости.

**Interfaces:** `Journal(store).append(event: dict, *, event_key: str) -> dict`; `.finish_session(task_id: str, session_id: str, payload: dict, *, closure_key: str, expected_revision: int, expected_context_revision: int) -> dict`; `.worklog(start_date: str, until_date: str, *, timezone: str) -> dict`; `.meeting_report(start_date: str, until_date: str, *, timezone: str, as_of_end: bool = False) -> dict`. `resolve_timezone(name: str, *, tzif_path: Path | None = None) -> tzinfo` использует проверенный источник или возвращает typed TZ_DATA_UNAVAILABLE.

- [ ] Добавить `test_finish_is_atomic_idempotent_and_not_completion`: одинаковый closure key/payload не дублирует записи; иной payload CONFLICT; stale checkpoint/context CAS не оставляет частичной записи. Reasons completed/awaiting_approval/blocked/paused/cancelled/interrupted закрывают сессию, только completed+APPROVED завершает задачу.
- [ ] Добавить `test_worklog_reports_events_and_carried_blockers`: late recorded event группируется по occurred_at, corrections сохраняют исходное; текущие и as-of блокеры различаются; сделано/проверено/published не смешиваются.
- [ ] Добавить `test_bucharest_midnight_and_dst`: `2026-11-11T22:30:00Z` относится к 12 ноября; spring `2026-03-29T00:30Z/01:30Z` даёт 02:30/04:30; fall `2026-10-25T00:30Z/01:30Z` даёт два 03:30 с разными offsets. Missing zone rules блокирует локальную сводку, UTC append работает.
- [ ] Получить TZif из проверенной официальной IANA tzdb release в рамках реализации; зафиксировать version/hash/provenance, включить asset в runtime packaging. Не полагаться на наличие системной IANA БД на Windows.
- [ ] Реализовать атомарный finish после проверки подготовленных artifact files, summaries/context revisions и stop integration. При аварии процесса восстанавливается последний checkpoint; stale running session помечается INTERRUPTED, не успешной.
- [ ] Добавить CLI `session finish`, `worklog`, `meeting-report`. Сохранить без изменения `daily/weekly` time semantics и отсутствие внешней отправки.

**Check:** targeted journal/timezones suites, существующие time/session tests, полный suite; тесты с двумя конкурентными closures, двумя задачами и границей суток без зависимости от timezone хоста.

## Task 10: Переносимые скиллы, установка и удаление активного MCP

**Files:** modify `scripts/build_assets.py`, `scripts/install.py`, `scripts/vendor_skills.py`, `scripts/audit_project_scope.py`, `scripts/ase.ps1`, `skills/*`, `agents/*`, `README.md`, `docs/workflow.md`, `docs/project-adoption.md`, `docs/ADR-002.md`; create `skills/task-intake/`, `skills/task-discovery/`, `templates/agents/claude/`, `templates/agents/copilot/`, `docs/ADR-004.md`, `tests/test_portable_install.py`; regenerate scoped `.agents/skills`/`.codex/agents` только в явно выбранном проекте. Remove future `ase/host.py` после переноса safeguards задачи 5.

**Interfaces:** `plan_install(targets: list[str], project: ProjectContext, *, runtime_home: Path) -> dict`; `apply_install(manifest: dict) -> dict`; `plan_mcp_cleanup(config_path: Path) -> dict`; `apply_mcp_cleanup(manifest: dict, *, authorization_ref: str) -> dict`. Install manifest содержит exact owned files, hashes, backups и целевую область; preview не меняет files/config.

- [ ] Добавить `test_one_source_three_targets`: 11 owned skills генерируются из одного канонического набора; target pointers/native roles соответствуют агенту, ссылки разрешимы, продуктовые тексты не требуют Superpowers или MCP.
- [ ] Добавить `test_install_preserves_other_projects_and_user_settings`: сохранились исходные configs/comments, no global activation, reapply idempotent, backups корректны, symlink/hardlink/type conflicts останавливают запись до изменений.
- [ ] Добавить `test_managed_mcp_cleanup_requires_explicit_authorization`: preview только показывает owned registration; cleanup удаляет ровно managed entry после authorization; чужие MCP/hook/auth/permission значения не меняются.
- [ ] Обновить canonical skills по разделу 11 дизайна и conditional references для вопросов, допущений, capability selection, stopping и finish. В universal workflow обычный финансовый/документный запрос не маршрутизируется в software.
- [ ] Заменить provider-specific vendor installer dependency на локальный stdlib копировщик проверенного pinned skill package либо доступный общий installer с теми же проверками. Независимый запуск скачанных scripts не допускается.
- [ ] Удалить активный MCP transport/registration option. Старый `--register-verifier` выдаёт явное сообщение об изменении и не меняет конфигурацию. Host-only tests заменить только по закрытой карте safeguards; ADR-002 сохранить как историю с superseded pointer.
- [ ] Выполнить runtime-only и project-scoped installation в disposable roots для каждого target; реальную пользовательскую установку пока не мигрировать.

**Check:** `test_portable_install.py`, `test_project_install.py`, `test_scope_audit.py`, generated-assets consistency и полный suite. Статический поиск активных references MCP отделяется от исторической документации и отрицательных тестов.

## Task 11: Согласованный экспорт и ручной перенос

**Files:** create `ase/transfer.py`, `tests/test_transfer.py`; modify `ase/projects.py`, `ase/__main__.py`, `docs/project-adoption.md`.

**Interfaces:** `export_project(project: ProjectContext, destination: Path, *, include_verbose: bool = False) -> dict`; `import_project(archive: Path, state_home: Path, *, root: Path, mode: str = "copy") -> ProjectContext`. Mode copy создаёт новый UUID с origin_project_id; mode restore сохраняет ID только при отсутствии конфликтующей локальной записи. Overwrite/merge отсутствуют.

- [ ] Добавить `test_export_is_consistent_during_wal_writes`: snapshot через SQLite backup содержит согласованную DB; included artifact hashes и manifest сверяются; активный незавершённый внешний эффект остаётся UNKNOWN.
- [ ] Добавить `test_import_rejects_escape_and_divergent_restore`: zip-slip, symlink/hardlink и нарушенные hashes отклоняются до активации; restore collision не перезаписывает данные.
- [ ] Добавить `test_copy_does_not_revive_approval`: новая копия получает новый project/workspace ID; origin/history доступны, но old receipts/approvals не разрешают действия в новом окружении.
- [ ] Реализовать явный export/import. Credentials и raw recordings/chats отсутствуют в пакете; verbose logs по умолчанию исключены. Пользователь предупреждается о review содержимого перед внешней передачей, сама передача не выполняется.
- [ ] Проверить backup/restore managed runtime как отдельный сценарий; старый runtime отказывается открывать v5, rollback использует pre-migration backup без фиктивного downgrade и без отката реальных ресурсов.

**Check:** targeted transfer tests, migration tests задачи 1 и полный suite.

## Task 12: Интеграционная приёмка и завершение миграции

**Files:** create `tests/test_workflow_scenarios.py`, `examples/universal_workflow_demo.py`, `docs/universal-workflow-validation.md`; update `docs/cli-compatibility.md`, `README.md`; не сохранять secrets/raw business content в checked-in evidence.

**Interfaces:** scenarios вызывают только публичные CLI/services, не устанавливают успешные verdicts прямым UPDATE БД. Живые traces хранятся отдельно от deterministic fixtures. Validation report связывает AC01–AC14 с exact command/session/artifact и ограничениями.

- [ ] Добавить сценарии: противоречивая встреча; неизвестное бизнес-правило с разрешённой независимой частью; stale cache после dirty change; повторная причина и exhausted budget; crash после внешнего эффекта; stop/finish/resume другим агентом; timezone/DST; проекты с одинаковыми именами; отсутствующий reviewer.
- [ ] В чистой среде без MCP выполнить workflow software от intake до meeting-report на каждом backend. AC и review — отдельные реальные сессии; после deliberate defect gate отвергает результат, после исправления требует свежие receipts.
- [ ] Выполнить infrastructure smoke в разрешённой disposable среде: фактический resource ID, before/after, postcondition, потеря ack и сверка. Протокольная VM fixture не считается живой проверкой настоящего гипервизора. Если живой гипервизор недоступен, соответствующий AC остаётся BLOCKED.
- [ ] Выполнить документный и исследовательский сценарии с источниками и проверяемыми результатами; mixed parent остаётся незавершённым, пока не покрыты все обязательства.
- [ ] Выполнить полный unittest suite на локальном Windows. CI matrix Windows/Ubuntu Python 3.11/3.13 остаётся проверкой runtime portability; удалённый CI считается выполненным только при реальном run. Live backend acceptance на других ОС не выводится из этих fixtures.
- [ ] Провести независимые AC и code review итогового diff; исправить P0/P1/P2 и повторить fresh evidence. Зафиксировать поддержку только фактически подтверждённых CLI/version/permission combinations.
- [ ] Сформировать migration manifest реальной установки, backup paths, известные blockers и короткий итог. Применять реальную миграцию только в рамках отдельной явной авторизации; этот план сам её не предоставляет.

**Check:** AC matrix ниже закрыта настоящими доказательствами; unavailable infrastructure/backend не переименованы в optional успех.

## Карта критериев и задач

| Критерий дизайна | Основные задачи |
|---|---|
| AC01 без MCP | 0, 5, 6, 10, 12 |
| AC02 три живых backend | 0, 5, 6, 12 |
| AC03 сохранение software checks | 5, 7, 8, 12 |
| AC04 выборочное обновление памяти | 1, 3, 4, 12 |
| AC05 согласование версии | 2, 8, 12 |
| AC06 meeting/partial scope | 2, 3, 12 |
| AC07 ограничение циклов и diagnostics | 2, 8, 12 |
| AC08 недублируемые эффекты | 8, 12 |
| AC09 finish/stop/checkpoint | 8, 9, 12 |
| AC10 даты и отчётность | 9, 12 |
| AC11 миграция и перенос | 1, 2, 11, 12 |
| AC12 portable skills и opt-in | 3, 10, 12 |
| AC13 профили и parent coverage | 2, 7, 12 |
| AC14 фактические ограничения среды | 0, 3, 5, 6, 12 |

## Публичные команды первой версии

Ниже будущие интерфейсы, не команды для запуска во время подготовки плана. Глобальные `--home` и `--project`/совместимый `--repo` предшествуют subcommand. Root выбирается явно; пример не предоставляет ему доступ.

```text
ase project register --root ROOT --kind KIND --timezone Europe/Bucharest
ase project attach PROJECT_ID --root ROOT --expected-revision N
ase --project PROJECT_ID capabilities
ase --project PROJECT_ID task create --file TASK_V2_JSON
ase --project PROJECT_ID discovery record TASK --file DISCOVERY_JSON --expected-revision N
ase --project PROJECT_ID task propose TASK --file PROPOSAL_JSON --expected-revision N
ase --project PROJECT_ID task approve TASK --proposal-hash HASH --user-message-ref REF --stage STAGE
ase --project PROJECT_ID task update-plan TASK --file EXECUTION_PLAN_JSON --expected-revision N
ase --project PROJECT_ID run TASK
ase --project PROJECT_ID resume TASK
ase --project PROJECT_ID check TASK
ase --project PROJECT_ID verify TASK
ase --project PROJECT_ID gate TASK
ase --project PROJECT_ID report TASK
ase --project PROJECT_ID session finish TASK --session-id SESSION --closure-key KEY --expected-revision N --expected-context-revision M --file CLOSURE_JSON
ase --project PROJECT_ID worklog --from YYYY-MM-DD --until YYYY-MM-DD --timezone Europe/Bucharest
ase --project PROJECT_ID meeting-report --from YYYY-MM-DD --until YYYY-MM-DD --timezone Europe/Bucharest
ase --project PROJECT_ID project export --destination ARCHIVE
ase project import --file ARCHIVE --root ROOT --mode copy
```

`--until` — исключающая верхняя граница локальной даты. `task approve --stage` повторяемый, сохраняет точный agreed stage set. `meeting-report --as-of-end` восстанавливает состояние на конец диапазона; без него активные на сегодня blockers отмечаются отдельно от событий периода. Старые time/report/publish commands сохраняются с прежними назначениями.

## Готовность плана и передача

При реализации сначала проверить соответствие исходного HEAD и инструкций фактическому checkout. При расхождениях обновить конкретные file mappings и повторить затронутый анализ, сохраняя согласованные требования.

Дизайн проверяется независимым builder check, план — self-review по покрытию R01–R12/AC01–AC14, согласованности interfaces, лимитам и ограничениям permissions. Документ не выдаёт реализацию, live acceptance или миграцию за выполненные действия.

Начинать Task 0 можно после явного разрешения пользователя на реализацию этого плана. Настройка отсутствующих CLI, миграция активной установки и доступ к внешней инфраструктуре выполняются только в пределах конкретно предоставленных полномочий; реальные blockers не закрываются предположениями.
