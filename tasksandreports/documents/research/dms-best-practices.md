# Лучшие решения систем управления документами (7 октября 2026)

Собрано по официальной документации продуктов и OWASP. Пункты, для которых
официального источника не нашлось, помечены «(общ.)» — это общеизвестные
возможности продукта.

## По системам

| Система | Решение | Источник |
|---|---|---|
| SharePoint | Основные и промежуточные версии (1.0 / 1.1); черновики видят только редакторы, читатели — последнюю опубликованную. Обязательное утверждение и извлечение файла для правки (check-out/check-in). | [support.microsoft.com](https://support.microsoft.com/en-us/office/plan-document-versioning-content-approval-and-check-out-controls-in-sharepoint-428b488e-2807-4ef0-b942-91cb09d8921c) |
| SharePoint | Типы содержимого: у вида документа свой набор полей, шаблон и политики. Document ID — постоянная ссылка, переживающая перенос. | (общ.) |
| SharePoint, Box, Alfresco | Метки хранения: документ объявляется записью, правка и удаление запрещены; сроки хранения по рубрике, уничтожение после проверки с протоколом; запрет удаления на время разбирательства (legal hold). | [netwrix.com](https://netwrix.com/en/resources/blog/records-management-in-sharepoint-how-does-it-work/), [support.box.com](https://support.box.com/hc/en-us/articles/18996033285523), [hub.alfresco.com](https://hub.alfresco.com/t5/alfresco-content-services-hub/records-management-user-guide/ba-p/289598) |
| Google Drive | Общие диски: файлы принадлежат команде, а не сотруднику. Панель активности. Доступ с датой окончания. | [chromeunboxed.com](https://chromeunboxed.com/google-drives-shared-drives-just-got-a-critical-long-overdue-security-feature) |
| Nextcloud | Антивирус ClamAV при загрузке и фоновая перепроверка. Прореживание старых версий. File Drop — папка «только загрузить». | [docs.nextcloud.com](https://docs.nextcloud.com/server/stable/admin_manual/configuration_server/antivirus_configuration.html) |
| Seafile | Отдельные журналы входов, доступа, правок и прав. | [proprivacy.com](https://proprivacy.com/cloud/review/seafile) |
| Confluence | Сравнение любых двух версий; утверждение привязано к версии, правка снова запускает утверждение. | [it.cornell.edu](https://it.cornell.edu/confluence/page-history-confluence) |
| Documentum | Жизненный цикл с проверками на входе и выходе состояния; автоматическая PDF-копия (рендиция) при смене состояния. | [argondigital.com](https://argondigital.com/blog/ecm/creating-a-simple-lifecycle) |
| M-Files | Представления — сохранённые поиски по полям вместо папок; класс документа задаёт поля и процесс. | [help.m-files.com](https://help.m-files.com/guides/managing-documents-with-views/) |
| DocuWare | Штампы: аннотация отдельным слоем, одновременно записывает значение в поле и двигает процесс. | [knowledgecenter.docuware.com](https://knowledgecenter.docuware.com/docs/docuware-web-client-stamps) |
| Paperless-ngx | OCR и архивный PDF/A; правила автоматической разметки; папка-приёмник; архивный номер, связывающий бумажный оригинал с копией через QR. | [docs.paperless-ngx.com](https://docs.paperless-ngx.com) |
| MasterControl, Qualio, Greenlight Guru | Запрос на изменение до черновика, связанный с CAPA; извещение об изменении выпускает и отменяет группу документов атомарно; обучение по новой редакции и вступление в силу после него; периодический пересмотр с исходом «без изменений». | [docs.qualio.com](https://docs.qualio.com/en/articles/12739506-change-management-options-in-qualio), [greenlight.guru](https://www.greenlight.guru/hubfs/Mastering%20Document%20and%20Change%20Management%20with%20Greenlight%20Guru%20Quality.pdf) |
| 21 CFR Part 11 | В подписи — ФИО, дата и время, смысл подписи («проверил / согласовал / утвердил»), видно на экране и в печати; подписание с повторным вводом пароля. | [opensecurityarchitecture.org](https://opensecurityarchitecture.org/frameworks/fda-21-cfr-11) |
| MasterControl | Контролируемые копии; печать с водяным знаком «Неконтролируемая копия», датой печати. | (общ.) |
| Directum RX, 1С:Документооборот | Регистрационный номер по маске вида документа; маршрут по регламенту вида; печатный лист согласования с замечаниями; лист ознакомления; номенклатура дел и сроки хранения. | [infostart.ru](https://infostart.ru/public/637276/), [directum.ru](https://www.directum.ru/webinars/drx-4.5_presentation.pdf) |

## ISO 9001 §7.5 — что требуется от управления документами

Документ доступен там, где нужен, пригоден и защищён; контролируются
распространение, доступ, хранение, читаемость, изменения, сроки хранения и
уничтожение; устаревшие документы не используются непреднамеренно
([разбор §7.5.3](https://www.thecoresolution.com/clause-7-5-3-iso-90012015-explained)).

1. Идентификация: обозначение по маске вида, изменение, дата введения — на
   каждом экземпляре.
2. Вид документа — справочник: цикл пересмотра, маршрут, срок хранения, поля.
3. Утверждение со смыслом подписи и печатным листом согласования.
4. Изменение по основанию (акт, протокол, аудит, CAPA), со сравнением версий.
5. Вступление в силу с даты, после ознакомления.
6. Распространение: рассылка и отчёт «кто не ознакомлен».
7. Устаревшее помечено «НЕДЕЙСТВУЮЩИЙ / заменён на …», вне поиска по
   умолчанию; ссылка «действующая редакция» ведёт на текущую версию.
8. Пересмотр — запись с исходом; следующая дата по циклу вида.
9. Сроки хранения и уничтожение по протоколу; удержание на время аудита.
10. Целостность: контрольная сумма, PDF/A, согласованная копия базы и файлов,
    проверка, что все файлы на месте.
11. Журнал: кто какую версию открыл и скачал — доказательство распространения.

## Безопасность хранения файлов (OWASP)

По [File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html):

- сверять сигнатуру файла с расширением; `Content-Type` браузера не доверять;
- Office-файлы — zip: перед разбором проверять размер после распаковки,
  степень сжатия и число частей; XML разбирать безопасно;
- файлы с макросами запрещать или помечать (`oletools`);
- антивирус при загрузке (ClamAV через `clamd`) и периодическая перепроверка;
- лимит размера тела на прокси равен лимиту приложения; частота загрузок и
  выгрузок ограничена;
- журнал скачиваний; контрольная сумма при загрузке, сверка при выдаче.
