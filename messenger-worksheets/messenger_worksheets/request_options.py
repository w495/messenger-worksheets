"""Проверить настройки одного запроса и ограничить режимы планирования и ревью."""

from __future__ import annotations

import re


REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
MODES = frozenset({"normal", "plan", "review"})
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z", flags=re.ASCII)
READONLY_DISABLED_FEATURES = (
    "image_generation", "hooks", "plugins", "multi_agent", "multi_agent_v2",
    "skill_mcp_dependency_install", "apps", "browser_use", "computer_use", "remote_plugin",
)


def validate_options(value: object = None) -> dict[str, str]:
    """Вернуть независимую проверенную копию опций только одного запроса.

    Параметр value — None либо словарь с необязательными model,
    reasoning_effort и mode. None превращается в пустой словарь. Имя модели
    допускает только ASCII буквы, цифры, точку, дефис и подчёркивание до 100
    символов; effort и mode проверяются по конечным наборам. Неизвестные поля,
    неверные типы и значения вызывают ValueError. Окружение, конфиги и входной
    словарь не изменяются; функций shell и сетевых обращений нет.
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or not value.keys() <= {"model", "reasoning_effort", "mode"}:
        raise ValueError("invalid request option fields")
    if any(not isinstance(item, str) for item in value.values()):
        raise ValueError("invalid request option type")
    if "model" in value and MODEL_NAME.fullmatch(value["model"]) is None:
        raise ValueError("invalid requested model")
    if "reasoning_effort" in value and value["reasoning_effort"] not in REASONING_EFFORTS:
        raise ValueError("invalid requested reasoning effort")
    if "mode" in value and value["mode"] not in MODES:
        raise ValueError("invalid request mode")
    return dict(value)


def effective_settings(
    options: object = None, *, model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, str | None]:
    """Объединить проверенные опции запроса с текущими model/effort worker.

    Параметры: options — опции одного job; model и reasoning_effort — уже
    полученные действующие значения worker либо None. Возвращает новый словарь
    model/reasoning_effort/mode. Запрос переопределяет только переданные поля;
    режим по умолчанию normal. Для неверных options выбрасывает ValueError.
    Не читает HOME/env и не сохраняет настройки соседних тредов или worker.
    """
    validated = validate_options(options)
    return {"model": validated.get("model", model),
            "reasoning_effort": validated.get("reasoning_effort", reasoning_effort),
            "mode": validated.get("mode", "normal")}


def option_instructions(options: object = None) -> str:
    """Вернуть инструкции выбранного режима для добавления к developer instructions.

    Параметр options — опции запроса. Для normal возвращает пустую строку,
    для plan/review — конкретную задачу и запрет изменений/внешних записей.
    ValueError передаётся из проверки опций. Это текстовая часть ограничения:
    native worker дополнительно обязан применить command_options, а direct
    Responses должен сохранять tools=[]; один prompt не считается песочницей.
    Не читает файлы, не запускает процесс и не изменяет состояние.
    """
    mode = validate_options(options).get("mode", "normal")
    if mode == "plan":
        return (
            " Режим /plan. Подготовь конкретный план выполнения запроса: шаги, "
            "нужные данные и способы проверки результата. Можно читать предоставленные "
            "материалы и доступные файлы для уточнения плана. Не выполняй саму задачу, "
            "не создавай и не меняй файлы, не устанавливай пакеты, не отправляй сообщения "
            "и не изменяй внешние системы. Возврати план текстом."
        )
    if mode == "review":
        return (
            " Режим /review. Проверь указанный код или результат, сообщи только "
            "подтверждённые дефекты с местом, условиями проявления и последствиями. "
            "Если дефектов не найдено, скажи об этом и назови пределы проверки. "
            "Если объект проверки отсутствует, запроси его вместо выдуманного ревью. "
            "Можно читать доступные материалы и файлы. Не исправляй код, не создавай "
            "и не меняй файлы, не устанавливай пакеты, не отправляй сообщения и "
            "не изменяй внешние системы. Возврати результаты ревью текстом."
        )
    return ""


def command_options(argv: list[str], options: object = None) -> list[str]:
    """Применить опции запроса к argv native Codex без shell и глобальных правок.

    Параметры: argv — готовый внутренний список ``binary exec ... -``;
    options — проверяемые настройки одного запроса. Возвращает новый argv.
    Model/effort заменяют прежние одноимённые overrides. Plan/review удаляют
    режим полного доступа и профили разрешений, выбирают read-only sandbox,
    ephemeral session и отключают hooks/plugins/apps/генерацию и делегирование.
    Resume/fork также удаляются,
    чтобы такой режим не продолжал сохранённую сессию с прежними разрешениями.
    Неверный argv или options вызывают ValueError. Ввод-вывод не выполняется;
    task worker обязан дополнительно не отдавать файлы для этих двух режимов.
    """
    validated = validate_options(options)
    if (not isinstance(argv, list) or len(argv) < 3 or argv[1] != "exec"
            or argv[-1] != "-" or any(not isinstance(item, str) for item in argv)):
        raise ValueError("invalid internal Codex command")
    readonly = validated.get("mode", "normal") in {"plan", "review"}
    output = argv[:2]
    index = 2
    while index < len(argv) - 1:
        item = argv[index]
        if item in {"-c", "--config"}:
            if index + 1 >= len(argv) - 1:
                raise ValueError("incomplete internal Codex config")
            key = argv[index + 1].split("=", 1)[0].strip()
            override_effort = "reasoning_effort" in validated and key == "model_reasoning_effort"
            override_model = "model" in validated and key == "model"
            unsafe_profile = readonly and (
                key in {"default_permissions", "sandbox_mode"}
                or key.startswith(("permissions.", "sandbox_workspace_write.", "sandbox_read_only."))
            )
            if not (override_effort or override_model or unsafe_profile):
                output.extend((item, argv[index + 1]))
            index += 2
            continue
        if item == "--model" and "model" in validated:
            if index + 1 >= len(argv) - 1:
                raise ValueError("incomplete internal model option")
            index += 2
            continue
        if item.startswith("--model=") and "model" in validated:
            index += 1
            continue
        if readonly and item in {"--sandbox", "resume", "fork"}:
            if index + 1 >= len(argv) - 1:
                raise ValueError("incomplete internal permission/session option")
            index += 2
            continue
        if readonly and (item in {"--ephemeral", "--full-auto", "--dangerously-bypass-approvals-and-sandbox"}
                         or item.startswith("--sandbox=")):
            index += 1
            continue
        if readonly and item in {"--enable", "--disable"} and index + 1 < len(argv) - 1 and argv[index + 1] in READONLY_DISABLED_FEATURES:
            index += 2
            continue
        output.append(item)
        index += 1
    additions = []
    if "model" in validated:
        additions.extend(("--model", validated["model"]))
    if "reasoning_effort" in validated:
        additions.extend(("-c", 'model_reasoning_effort="' + validated["reasoning_effort"] + '"'))
    if readonly:
        additions.extend(("--sandbox", "read-only", "--ephemeral"))
        for feature in READONLY_DISABLED_FEATURES:
            additions.extend(("--disable", feature))
    return [*output[:2], *additions, *output[2:], "-"]
