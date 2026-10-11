# unity-asset-extraction

CLI и Codex Skill для локальной инвентаризации и извлечения Unity AssetBundle и локальных Addressables. Поддерживает файлы, каталоги, ZIP/zipmod, JSON catalog и cache; сохраняет SHA исходников и результатов, фактические типы объектов и отчёт полноты в `manifest.json`.

## Состав репозитория

```text
unity-asset-extraction/
  SKILL.md
  agents/openai.yaml
  scripts/extract_unity_assets.py
  scripts/ExportUnityAssets.ps1
  references/usage_ru.md
tests/test_extract_unity_assets.py
README.md
LICENSE
.gitignore
```

Подробный контракт и ограничения: [references/usage_ru.md](unity-asset-extraction/references/usage_ru.md).

## Требования

Контроллер использует Python 3.10+ и стандартную библиотеку. Для подсчёта объектов и экспорта `Texture2D` в PNG нужен уже установленный UnityPy в выбранном Python; ранее проверялась версия 1.25.4. Контроллер не устанавливает зависимости. Без UnityPy экспорт через AssetRipper возможен, но inventory/coverage остаются неполными.

AssetRipper — внешний доверенный executable, явно заданный через `--assetripper`; бинарник в репозиторий не входит. Реализован контракт API AssetRipper.GUI.Free 2.0.0.0 с закреплённым SHA256:

```text
f7873b92063ba0551b9a9513cfa1554ea8b9bf8eb49e218076bc3a6d794864f5
```

Несовпадение SHA запрещает запуск. `--backend-sha256` допускает другой executable только после отдельной предварительной проверки его совместимости владельцем; указание нового SHA само по себе её не подтверждает. До `/LoadFile` контроллер применяет через `/Settings/Update` настройки `ShaderExportMode=Yaml`, `ScriptContentLevel=Level0`, `ScriptExportMode=DllExportWithoutRenaming`, `ExportUnreadableAssets=true`, затем использует `/Export/UnityProject`.

## Запуск из корня репозитория

Пути в примерах обозначают локальные файлы владельца. Каждый `--output` должен быть новым, отсутствующим каталогом; по умолчанию размещай его вне `Assets` и любых каталогов автоматического импорта. `--input` можно повторять.

```powershell
python -B unity-asset-extraction/scripts/extract_unity_assets.py --help

# Inventory: отдельный файл, каталог cache, локальный catalog и zipmod
python -B unity-asset-extraction/scripts/extract_unity_assets.py --input './inputs/textures.bundle' --input './inputs/cache' --input './inputs/catalog.json' --input './inputs/mod.zipmod' --output './exports/inventory-new' --inventory-only

# PNG через UnityPy выбранного Python
python -B unity-asset-extraction/scripts/extract_unity_assets.py --input './inputs/textures.bundle' --output './exports/png-new' --backend unitypy

# Native YAML и связанные assets через внешний AssetRipper
python -B unity-asset-extraction/scripts/extract_unity_assets.py --input './inputs/shaders.bundle' --output './exports/yaml-new' --backend assetripper --assetripper './backend-bin/AssetRipper.GUI.Free.exe'

# Windows wrapper: массив путей с пробелами и явный Python
./unity-asset-extraction/scripts/ExportUnityAssets.ps1 -InputPath @('./inputs/first bundle.bundle', './inputs/second bundle.bundle') -OutputPath './exports/wrapper-new' -PythonPath 'C:/Tools/Python/python.exe' -Backend assetripper -AssetRipperPath './backend-bin/AssetRipper.GUI.Free.exe'
```

Для JSON Addressables передавай также каталог с локальными bundles/cache, чтобы включить его в разрешённые корни и фактический экспорт. Wrapper передаёт аргументы массивом без `Invoke-Expression` и возвращает exit code контроллера. Если UnityPy установлен в отдельном окружении, выбери его Python, в wrapper — через `-PythonPath`.

## Установка в Codex

Скопируй **только вложенную папку `unity-asset-extraction`** в каталог `skills` внутри `CODEX_HOME`, если эта переменная задана; иначе — в `~/.codex/skills`, в том числе для Codex App на Windows. Итоговая папка skill должна непосредственно содержать `SKILL.md`, `agents`, `scripts` и `references`. Корень репозитория и `tests` копировать туда не нужно.

После обнаружения skill в Codex вызывай его явно как `$unity-asset-extraction`. Если неявный выбор skills включён, подходящий запрос на локальное извлечение также может активировать его. Установка skill, установка зависимостей и последующий Unity import — отдельные действия владельца.

## Границы и защита

- Исходники используются read-only; SHA проверяются до и после обработки. Существующий output, пересечение inputs/output, UNC и известные symlink/junction запрещены; перезаписи нет. Связанные результаты одного source сохраняются вместе в namespace полного SHA.
- Локальные пути catalog разрешаются только внутри явно переданных input roots; для файлового input корнем служит его родитель. Remote IDs не скачиваются. `catalog.bin` не поддерживается, адреса и dependency topology JSON catalog не декодируются: такой catalog остаётся `partial`.
- ZIP проверяется до staging: traversal, ссылки, шифрование, коллизии путей и превышение budgets отвергаются. Ограничены число файлов, входные/выходные байты, коэффициент распаковки, логи и время; вложенные архивы расходуют общие budgets. DLL и скрипты из inputs не исполняются.
- На Windows worker и дочерний backend принадлежат собственному kill-on-close Job; на POSIX — собственной process group. Cleanup останавливает только принадлежащие запуску процессы. Это не OS sandbox: возможны overshoot budgets и задержка финального hashing/cleanup после deadline; жёсткая квота памяти/диска не обещается. Используй приватный локальный output без конкурентной подмены каталогов.
- Shader экспортируется как native YAML/GPU data. Авторский HLSL не восстанавливается; Dummy/no-op shader не создаётся. Lossless export всех типов assets и готовность к Unity import не обещаются. Связи между отдельными bundles автоматически не восстанавливаются; последующий import требует отдельной проверки.

## Совместимость после экспорта

Полнота экспорта (`complete`/`partial`) не является проверкой совместимости с Unity. Слепой импорт всего экспортированного набора запрещён. Skill сам не запускает Unity и не импортирует assets.

Наблюдался конкретный случай: native Shader YAML Unity 2018.4, экспортированный AssetRipper 2.0.0.0, при импорте в Unity 6000.6 вызвал schema assertions, рост лога и зависание/остановку Editor. Версионные схемы сериализации могут различаться; этот случай не означает несовместимость всех Shader YAML и не отменяет проверенные результаты других форматов и режимов. Загрузка AssetBundle — отдельный путь и не доказывает совместимость самостоятельных экспортированных ассетов.

Перед использованием native Shader YAML сохрани оригиналы и экспорт вне каталогов автоматического импорта и проверь копию одного кандидата с необходимыми зависимостями в отдельном контролируемом проекте целевой версии Unity. Сначала закрой Editor рабочего проекта; параллельный Editor для уже открытого рабочего проекта не создавай. До запуска задай предел размера лога и времени и наблюдай за логом. При первой schema ошибке или достижении предела останови проверку и её Editor, сохрани ограниченный лог и исходные данные; не вызывай `SaveAssets` для native YAML. Зафиксируй версии источника, AssetRipper и Unity, SHA кандидата и результат. Успех одного кандидата не разрешает импорт всего набора.

Save guard не делает несовместимые данные пригодными. Editable HLSL не обещается. Подробная процедура — в [usage_ru.md](unity-asset-extraction/references/usage_ru.md).

## Результат и проверка

Проверяй в `manifest.json` SHA до/после, `source_unchanged`, actual type counts, output hashes, ошибки, skips и unresolved dependencies. HTTP 200 и наличие файлов не доказывают полноту.

| Exit code | Статус | Значение |
|---|---|---|
| `0` | `complete` | Все известные inputs обработаны в выбранном режиме без выявленных gaps; inventory complete не означает export complete. |
| `2` | `partial` | Получена содержательная часть результата, но есть unsupported types, ошибки или пробелы coverage/dependencies. |
| `1` | `failed` | Нет содержательного результата либо не пройдены preflight guards; при preflight output не создаётся. |

AssetRipper export остаётся `partial`, пока не доказано object/dependency coverage; нулевой export не становится `complete`. UnityPy экспортирует только `Texture2D`, поэтому остальные содержательные типы требуют другого backend и отражаются как gap.

После hardening независимая проверка в корне репозитория подтвердила: **23 unit tests — PASS**, `quick_validate` — skill valid; SHA256 фактических исходников проверены. Реальный offline export после hardening дал **100 выходных файлов**, включая **13 native Shader YAML assets** и **6 PNG**. SHA256 двух оригинальных входных файлов остались неизменными. Итоговый статус — **`partial`**: остались **2 unresolved dependencies**, а покрытие объектов backend доказано не полностью.

Unit tests, `quick_validate` и фактический export подтверждают только проверенные свойства и результат этого запуска. Они не доказывают успешный Unity import, готовность игрового персонажа, восстановление авторского HLSL или полную Addressables topology. Binary catalog (`catalog.bin`) по-прежнему не поддерживается.

После сборки исходников тесты запускаются из корня репозитория:

```powershell
python -B -m unittest discover -s tests -p test_extract_unity_assets.py
```

Unit tests не заменяют фактический export smoke на разрешённых входах.

## Лицензия и публичные материалы

[MIT](LICENSE) относится к нашему независимому универсальному коду. Зависимости лицензируются отдельно: лицензия AssetRipper GPL не заменяется MIT; его код, бинарники и извлечённые Editor patches сюда не копируются. UnityPy остаётся внешней зависимостью со своей лицензией. Лицензия проекта не расширяет права на пользовательские inputs и извлечённые assets.

Публичный пакет не содержит исходные образцы, screenshots, бинарники backend, source game, GDD или приватные пути. Входные данные, exports и чувствительные локальные артефакты исключены через `.gitignore`; перед публикацией всё равно проверяй состав файлов.
