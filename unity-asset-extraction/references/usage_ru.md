# Локальное извлечение Unity assets

Контроллер требует Python 3.10+ и стандартную библиотеку. UnityPy — необязательный уже установленный пакет выбранного Python: он даёт object counts и экспорт Texture2D в PNG. Без него AssetRipper может экспортировать assets, но inventory/coverage останутся partial. Скрипт ничего не устанавливает.

## Команды

Пути ниже относительны к папке skill; inputs можно повторять. Output должен отсутствовать; по умолчанию размещай его вне `Assets` и любых каталогов автоматического импорта.

```powershell
python -B scripts/extract_unity_assets.py --help
python -B scripts/extract_unity_assets.py --input D:/Data/cache --input D:/Data/mod.zipmod --output D:/Temp/inventory-new --inventory-only
python -B scripts/extract_unity_assets.py --input D:/Data/textures.bundle --output D:/Temp/png-new --backend unitypy
python -B scripts/extract_unity_assets.py --input D:/Data/shaders.bundle --output D:/Temp/yaml-new --backend assetripper --assetripper D:/Tools/AssetRipper.GUI.Free.exe
./scripts/ExportUnityAssets.ps1 -InputPath @('D:/Data/one.bundle','D:/Data/two.bundle') -OutputPath D:/Temp/export-new -PythonPath D:/Env/Scripts/python.exe -Backend assetripper -AssetRipperPath D:/Tools/AssetRipper.GUI.Free.exe
```

Для inventory и экспорта используй Python окружения с существующим UnityPy; глобальный Python без пакета не становится parser автоматически. Wrapper возвращает тот же exit code и передаёт пути как аргументы без Invoke-Expression.

## AssetRipper: проверенный контракт

По умолчанию допускается executable SHA256 `f7873b92063ba0551b9a9513cfa1554ea8b9bf8eb49e218076bc3a6d794864f5` — проверенный AssetRipper.GUI.Free v2.0.0.0. `--backend-sha256` позволяет владельцу указать SHA другого заранее проверенного совместимого executable; это не автоматическое подтверждение API/качества новой версии. Helper evidence: Export-UnityAssets.ps1 SHA `e601efe6fde9b1c300ce75efdfb27e8b141123cc433f49df91ce8d9c04b30c5d`. Никаких DLL из inputs не исполняется; неизвестный executable не запускается при несовпадении SHA.

Запуск без shell: `--headless --port N --log-path LOG`, hidden на Windows. Только own loopback HTTP; до пяти redirect допускаются строго на тот же `127.0.0.1:port`, внешние отвергаются до запроса. Реальный settings API возвращает 302 (готовый helper также следовал redirect). GET `/` ожидает 200; до LoadFile отдельные POST form `/Settings/Update` устанавливают ShaderExportMode=Yaml, ScriptExportMode=DllExportWithoutRenaming, ScriptContentLevel=Level0, ExportUnreadableAssets=true. Далее POST `/LoadFile` и `/Export/UnityProject`, поле `path`. Directory/group/catalog endpoints не предполагаются. Каждому source соответствует отдельный процесс и namespace SHA. Source dependencies между разными bundles автоматически не объединяются; неподтверждённое покрытие фиксируется.

Texture/material/shader и связанные outputs, созданные backend для одного входа, сохраняются вместе. Авторский HLSL не восстанавливается: native Shader YAML/GPU остаётся фактическим форматом.

## Совместимость после экспорта

Проверка `complete`/`partial` описывает полноту экспорта, а не совместимость с Unity. Слепой импорт всего экспортированного набора запрещён; skill сам не запускает Unity и не импортирует assets.

Наблюдался конкретный случай: native Shader YAML Unity 2018.4, экспортированный AssetRipper 2.0.0.0, при импорте в Unity 6000.6 вызвал schema assertions, рост лога и зависание/остановку Editor. Схемы сериализации разных версий могут различаться. Это не доказывает несовместимость всех Shader YAML и не отменяет проверенные результаты других форматов и режимов. Загрузка исходного AssetBundle — отдельный путь; её успех не подтверждает совместимость самостоятельных экспортированных ассетов.

Перед использованием native Shader YAML владелец выполняет отдельную проверку:

1. Сохрани оригиналы и экспорт неизменными вне каталогов автоматического импорта. Выбери один кандидат и только необходимые ему зависимости; используй копию для проверки.
2. Подготовь отдельный контролируемый проект в целевой версии Unity. Сначала заверши работу и закрой Editor рабочего проекта: не создавай параллельный Editor для уже открытого рабочего проекта.
3. До запуска задай предел размера лога и времени проверки, наблюдай за логом. При первой schema ошибке или достижении любого предела останови проверку и её Editor, сохрани ограниченный лог и исходные данные. Не вызывай `SaveAssets` для native YAML.
4. Зафиксируй версии источника, AssetRipper и целевого Unity, SHA кандидата и результат. Успех одного кандидата относится только к проверенному случаю; остальные assets требуют своей проверки перед использованием.

Save guard может предотвращать сохранение, но не делает несовместимые данные пригодными. Editable HLSL этот путь не восстанавливает.

## Форматы и границы

- UnityFS/UnityRaw/UnityWeb определяются по header, включая extensionless cache. Header detection не доказывает успешный parse.
- `.assets`, `.resS`, `.resource` передаются parser/backend; одиночные companions не обещают полный набор objects.
- ZIP/zipmod полностью проверяется до staging: traversal, UNC/drive/NUL, ссылки, encryption, duplicate/case/file-directory collisions и ratio. Entries извлекаются только в owned staging, сохраняются archive/entry SHA и origin `archive!entry`. Архивы внутри архивов тоже ограничены общими budgets. DLL/скрипты из архива только unsupported inventory.
- JSON Addressables: читается `m_InternalIds`, локальные пути разрешаются только внутри explicit input roots (для file input — его parent). HTTP/прочие remote IDs не загружаются; missing/remote/runtime placeholders — unresolved. Address/dependency topology не декодируется, поэтому catalog остаётся partial даже при известных local IDs. Для фактического экспорта включи также directory с bundles.
- `catalog.bin` — unsupported/partial (или failed без других известных inputs). Найденный cache bundle не означает восстановленный catalog.
- Неизвестные входы явно skipped; no fake serialization/decompilation.

## Budgets и process lifecycle

Параметры Python (в wrapper: TimeoutSeconds/MaxFiles/MaxBytes/MaxOutputBytes/MaxZipRatio/MaxLogBytes):

| Flag | Default | Смысл |
|---|---:|---|
| `--timeout` | 120 s | Общий deadline inventory/worker/output hashing |
| `--max-files` | 10000 | Source+archive entry count, и отдельный output count |
| `--max-bytes` | 2147483648 | Source+uncompressed archive bytes |
| `--max-output-bytes` | 4294967296 | Суммарные export bytes всех source namespaces, включая активный worker |
| `--max-zip-ratio` | 200 | Uncompressed/compressed ratio на entry |
| `--max-log-bytes` | 8388608 | Worker stdout/stderr, result JSON и catalog JSON |

Все budgets положительные; float-параметры `--timeout` и `--max-zip-ratio` обязаны быть конечными: nan/inf отвергаются Python CLI до создания output. Backend SHA вычисляется один раз в preflight и переиспользуется в provenance; worker повторно проверяет executable перед запуском. Deadline после первоначального source scan проверяется до создания output. Если output уже создан, ошибки setup/обработки проходят через manifest и owned cleanup.

Final source rehash, bounded final output inventory и owned cleanup выполняются также после deadline: они могут добавить время на чтение bounded input/output. Это отдельная проверка неизменности, а не гарантия жёсткого времени завершения. Filesystem writes проверяют ancestors/junction/symlink и containment; inputs/output overlap, существующий output и UNC запрещены. Нет overwrite: повторный запуск требует новый output. Full source SHA namespaces детерминированы; идентичные bundles deduplicated. Manifest не обещает byte-identical generated backend outputs.

Если после закрытия Job файловые handles ещё освобождаются, cleanup повторяет удаление owned staging при PermissionError не дольше одной секунды, каждый раз заново проверяя пути. Постоянный отказ остаётся ошибкой manifest.

UnityPy PNG называются `<object_ordinal>_<path_id>.png` внутри source SHA namespace. Ordinal — индекс объекта в порядке parser данного входа; это не выдуманный ID serialized file и не обещание стабильности порядка между версиями parser. `inputs[].object_exports` связывает ordinal/path_id/type с относительным PNG path. Поэтому одинаковые file-local path IDs разных serialized files не перезаписывают PNG; каждый файл создаётся эксклюзивно.

Worker stdout/stderr идут в owned файлы; контроллер наблюдает их размер и deadline без reader threads/Future. Job logs (stdout/stderr/backend log/result, без controller request) суммируются отдельно по MaxLogBytes и не расходуют asset MaxFiles/MaxOutputBytes. Активный export и ранее созданные namespaces суммируются по asset budgets. Обход потоковый через scandir: deadline проверяется на каждой entry, count/bytes — на каждом файле, без предварительного списка всего каталога. На Windows Python worker ждёт handshake до назначения kill-on-close Job; дочерний backend принадлежит этому Job. На POSIX используется owned process group. Cleanup закрывает Job/останавливает только own processes; broad taskkill отсутствует. Это не sandbox против hostile OS escape. Между обходами пауза 50 ms; обход и OS scheduling тоже занимают время, поэтому overshoot возможен, hard filesystem/memory quota не обещается. Проверка путей предотвращает известные links, но не защищает от конкурентной подмены директорий внешним процессом; используй приватный локальный output.

## Manifest и exit codes

`manifest.json` содержит schema/operation/status, input origin/SHA before+after/source_unchanged, kind, parser version/type_counts, namespace; outputs relative path/SHA/bytes/native_class_id/format; backend SHA/settings/provenance, budgets, errors/skips/unresolved_dependencies. Output class IDs читаются из actual YAML `!u!`, object counts — из UnityPy, расширения не подменяют типы. Staging и worker logs удаляются при cleanup; error summary остаётся в manifest.

- `0 complete`: все известные inputs успешно обработаны в выбранном режиме, issues отсутствуют. Для UnityPy export допустим Texture2D-only набор с AssetBundle metadata и без externals; всё прочее partial. Inventory complete не означает export complete.
- `2 partial`: известная часть обработана, но есть unsupported/error/dependency/coverage gap.
- `1 failed`: ни один вход не дал содержательного inventory/export; также preflight guard failure. При preflight output вообще не создаётся.

AssetRipper export намеренно partial пока нет доказанной object/dependency coverage: successful HTTP/file export сам по себе не делает complete. Нулевой export никогда не complete. Пустой input directory — failed. При timeout частичные files могут остаться внутри output, они не принимаются за готовый результат. SHA mismatch исходников — явная ошибка.

## Проверка bundle перед установкой

Из repository root:

```powershell
python -B -m unittest discover -s Tools/AssetExtraction/tests -p test_extract_unity_assets.py
python -B -X utf8 '<skills-root>/.system/skill-creator/scripts/quick_validate.py' Tools/AssetExtraction/unity-asset-extraction
```

`<skills-root>` замени на локальный каталог skills. Тесты создают только приватные fixtures под `D:/Temp/asset-extraction-tests` на Windows; parser/API fault doubles проверяют реальные subprocess/report/cleanup эффекты. Unit tests не заменяют actual AssetRipper export smoke на разрешённых bundles и отдельную проверку совместимости с Unity. Глобальная установка, скачивание, публикация proprietary данных и Unity import в эти команды не входят.
