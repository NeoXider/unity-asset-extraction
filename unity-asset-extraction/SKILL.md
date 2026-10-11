---
name: unity-asset-extraction
description: "Локальная инвентаризация и извлечение Unity AssetBundle, serialized assets, ZIP/zipmod и Addressables catalog/cache через готовые AssetRipper и UnityPy с SHA и честным отчётом полноты. Использовать для offline extraction, не для Unity import или восстановления авторского HLSL."
---

Используй этот skill для локального анализа и экспорта разрешённых Unity assets. Не устанавливай пакеты глобально и не скачивай зависимости автоматически.

Прочитай [references/usage_ru.md](references/usage_ru.md): там полный CLI, требования backend, ограничения форматов и трактовка manifest.

1. Выбери существующий Python с UnityPy для type inventory/Texture2D. Для Shader/Material/сцен используй явно указанный доверенный AssetRipper с проверенным SHA и API-контрактом.
2. Запусти `scripts/extract_unity_assets.py --help`, затем inventory в **новый** output. Для экспорта запусти второй проход в другой новый output; Windows wrapper — `scripts/ExportUnityAssets.ps1`.
3. Проверь `manifest.json`: исходные SHA до/после, actual type counts, output hashes, errors/skips/unresolved dependencies. `partial` и `failed` не выдавай за полный результат; HTTP 200 не доказывает coverage.
4. Shader сохраняется как native YAML/GPU data. Editable HLSL не заявляется восстановленным. Не заменяй его Dummy, no-op или переименованными compiled bytes.

Источники read-only. Не выполняй DLL из входов, не загружай remote IDs, не импортируй результат в Unity этим skill и не публикуй proprietary bytes. AssetRipper может создавать собственные .meta/.unity/.prefab только внутри отдельного output; project assets не меняются. Связанные assets остаются вместе в namespace полного SHA исходника. Установка skill и последующий Unity import — отдельные действия владельца.
