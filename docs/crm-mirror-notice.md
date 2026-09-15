# CRM analytics notice — texts

Approved by the owner on 2026-09-15 (owner decision D-7 as changed that day, G0 question
Q12). In milestone M4b these strings move into `web/messages/ru.json` and
`web/messages/en.json` and `CrmDataNotice.tsx` renders them, so wording changes happen here
first and the code copies them.

Page names match the current catalogue: navigation "По сделкам" / "Источники".

## When it shows, and to whom

The notice informs; it gates nothing. CRM storage starts on every eligible portal as soon as
the release ships, whether or not an administrator has seen the notice. This replaces the
14-day wait D-7 originally required.

| Portal | Who sees it | Where |
| --- | --- | --- |
| Installed before the CRM mirror release | Administrators only | A banner on the dashboard until each one dismisses it, and a block in settings |
| Installed after the release | Administrators only | The same banner and settings block. The install page closes the slider at once, so text there would flash past unread; section 5 is the short form for places with one line of room |

Employees who are not administrators see nothing new.

## 1. Banner (existing installs)

**ru**

> **Отчёты «По сделкам» и «Источники» теперь работают по-новому**
>
> Приложение хранит на своём сервере копию минимального набора данных CRM вашего портала:
> стадии, суммы, ответственных, даты и UTM-метки сделок и лидов. Названия сделок и лидов,
> контакты клиентов, комментарии и пользовательские поля не сохраняются. Поэтому отчёты
> открываются быстрее, за любой период до года и без обращения к API Битрикс24 при каждом
> открытии.
>
> Если вы не хотите, чтобы данные CRM хранились, отключите аналитику CRM — сохранённые
> данные будут удалены. Отчёты по звонкам продолжат работать без изменений.
>
> [Политика конфиденциальности] [Отключить аналитику CRM] [Понятно]

**en**

> **The Deals and Sources reports now work differently**
>
> The app keeps a copy of a minimal set of your portal's CRM data on its server: stages,
> amounts, responsible employees, dates and UTM tags of deals and leads. Deal and lead
> titles, client contacts, comments and custom fields are not stored. That is why reports
> open faster, for any period up to a year, without calling the Bitrix24 API on every open.
>
> If you do not want CRM data stored, turn CRM analytics off and the stored data is deleted.
> Call reports keep working as they do now.
>
> [Privacy policy] [Turn off CRM analytics] [Got it]

"Понятно" / "Got it" hides the banner for that administrator. The settings block stays.

## 2. Confirmation before turning it off

**ru**

> **Отключить аналитику CRM?**
>
> Данные CRM этого портала не будут храниться, а уже сохранённые будут удалены. Отчёты
> «По сделкам» и «Источники» станут недоступны. Включить аналитику снова можно в
> настройках приложения.
>
> [Отключить] [Отмена]

**en**

> **Turn off CRM analytics?**
>
> This portal's CRM data will not be stored, and anything already stored will be deleted.
> The Deals and Sources reports will become unavailable. You can turn analytics back on in
> the app settings.
>
> [Turn off] [Cancel]

## 3. Settings, while it is off

**ru**

> Аналитика CRM отключена {date}. Данные CRM портала не хранятся, отчёты «По сделкам» и
> «Источники» недоступны. [Включить аналитику CRM]

**en**

> CRM analytics was turned off on {date}. No CRM data of this portal is stored, and the
> Deals and Sources reports are unavailable. [Turn on CRM analytics]

`{date}` is the day it was turned off, formatted in the viewer's locale.

## 4. Settings, while it is on

**ru**

> Аналитика CRM включена. Приложение хранит копию минимального набора данных CRM — состав
> описан в разделе 2 политики конфиденциальности. [Отключить аналитику CRM]

**en**

> CRM analytics is on. The app keeps a copy of a minimal set of CRM data, listed in
> section 2 of the privacy policy. [Turn off CRM analytics]

## 5. Short form (one line of room)

**ru**

> Для отчётов «По сделкам» и «Источники» приложение хранит копию минимального набора
> данных CRM. Отключить это можно в настройках приложения.

**en**

> For the Deals and Sources reports the app keeps a copy of a minimal set of CRM data. You
> can turn this off in the app settings.

## 6. Marketplace listing, "What's new"

**ru**

> Отчёты «По сделкам» и «Источники» теперь строятся из копии данных CRM на сервере
> приложения: любой период до года, без ограничений API Битрикс24. Сохраняются только
> стадии, суммы, ответственные, даты и UTM-метки сделок и лидов; названия, контакты
> клиентов и комментарии не сохраняются. На уже установленных порталах изменение действует
> сразу; администратор может отключить аналитику CRM в настройках приложения. Политика
> конфиденциальности обновлена 15 сентября 2026 г. Рекомендуем устанавливать приложение от
> имени администратора портала.

**en**

> The Deals and Sources reports are now built from a copy of CRM data on the app's server:
> any period up to a year, without Bitrix24 API limits. Only stages, amounts, responsible
> employees, dates and UTM tags of deals and leads are stored; titles, client contacts and
> comments are not. On portals where the app is already installed the change applies at
> once; an administrator can turn CRM analytics off in the app settings. The privacy policy
> was updated on 15 September 2026. We recommend installing the app as a portal
> administrator.

## Decisions behind the wording

Both approved by the owner on 2026-09-15, and binding on the code:

1. **Turning CRM analytics off makes both reports unavailable at once**, the live reads
   included, so "станут недоступны" is true from the first release rather than only after
   the live reads are removed.
2. **A "turn back on" button ships in M4b.** Turning it back on starts storage at once.
