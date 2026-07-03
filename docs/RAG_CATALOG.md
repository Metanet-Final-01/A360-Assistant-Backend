# A360 RAG 지식베이스 카탈로그

> 자동 생성 문서 (2026-07-03) — 원천: `data/ingest/packages.json` (패키지 JAR `package.json` 파싱 결과)
> 지식베이스 전체는 pgvector `rag_documents` 테이블에 임베딩과 함께 적재되어 있으며,
> 팀원 복원 방법은 [app/ingest/TEAM_SETUP.md](../app/ingest/TEAM_SETUP.md) 참고.

## 개요

| source_type | 건수 | 내용 |
|---|---|---|
| doc_page | 1,295 | A360 공식 문서 페이지 (Fluid Topics API 수집) |
| action_schema | 351 | 액션별 파라미터 스키마 (패키지 JAR 파싱) |
| package_overview | 57 | 패키지 개요 |
| bot_example | 20 | 실제 봇 구성 예제 (GitHub 공개 봇) |
| **합계** | **1,723** | |

패키지 **57개**, 액션 **368개** (파라미터 스키마 보유 357개).
파라미터 표기: `라벨(타입*: 선택지1/선택지2)` — `*`는 필수, 선택지는 RADIO/SELECT 타입의 옵션 (6개 초과 시 `…`).

## 패키지 목록

| 패키지 | 라벨 | 액션 수 | 설명 |
|---|---|---|---|
| Excel_MS | Excel 고급 | 53 | 이전 Excel 형식과 고급 Excel 작업을 지원합니다. MS Excel 프로그램을 설치해야 합니다. |
| WebAutomation | Web Automation | 50 | Provides commands for Browser Automation actions. |
| String | 문자열 | 16 | 문자열 작업을 수행하기 위한 동작을 제공합니다. |
| Excel | Excel 기본 | 15 | MS Excel 프로그램을 설치할 필요 없이 .xlsx 파일에 대한 빠른 스프레드시트 작업이 필요합니다. |
| Trello | Trello | 14 | Provides actions to integrate with Trello |
| Email | 이메일 | 13 | 이메일 작업을 수행하기 위한 활동을 제공합니다. |
| AWSDynamoDB | AWS DynamoDB | 11 | Provides actions for AWS DynamoDB operations. |
| Slack | Slack | 11 | Provides actions to integrate with Slack |
| File | 파일 | 10 | 파일 작업을 수행하기 위한 동작을 제공합니다. |
| XML | XML: | 10 | xml 작업을 수행하기 위한 동작을 제공합니다. |
| List | 목록 | 9 | 목록 데이터 유형의 변수에 대해 다양한 작업을 수행합니다. |
| Folder | 폴더 | 8 | 폴더 작업을 수행하기 위한 동작을 제공합니다. |
| Browser | 브라우저 | 8 | 브라우저 작업을 수행하기 위한 동작을 제공합니다. |
| Twitter | A2019DemoPackage | 8 | A2019DemoPackage 작업에 대한 동작을 제공합니다. |
| PDF | PDF | 8 | PDF 파일 작업을 수행합니다. |
| Datetime | 날짜 시간 | 7 | 날짜/시간 변수의 값 업데이트 및 비교와 같은 날짜/시간 값에 대해 다양한 작업을 수행할 수 있습니다. |
| GoogleMaps | Google Maps | 7 | Google Maps Actions |
| Boolean | 부울 | 6 | 부울 작업을 수행하기 위한 활동을 제공합니다. |
| Number | 수 | 5 | 수 작업을 수행하기 위한 동작을 제공합니다. |
| System | 시스템 | 5 | 시스템의 잠금, 로그오프, 재시작 및 종료 작업을 자동화합니다. |
| Dictionary | 사전 | 5 | 사전 작업을 수행하기 위한 활동을 제공합니다. |
| Rest | REST Web Services | 5 | 웹 서비스 작업을 수행하기 위한 동작을 제공합니다. |
| HTMLParser | HTML Parser | 5 | Provides actions for HTML parsing. |
| Kore AI NLP | Kore AI NLP | 5 | Kore AI NLP |
| Locale | Locale | 5 | Provides actions for localization operations. |
| Salesforce | Salesforce | 5 | Actions for interacting with Salesforce Objects and the Salesforce API |
| FileFolderAttributes | A2019DemoPackage | 5 | A2019DemoPackage 작업에 대한 동작을 제공합니다. |
| ErrorHandler | 오류 처리기 | 4 | Bot에서 오류를 처리하는 명령을 제공합니다. |
| MSWordPackage | MS Word | 4 | Use this package to create and modify MS Word Document |
| DataRobot Models | DataRobot Models | 4 | DataRobot Integration with Model Deployments |
| DLL | DLL | 4 | DLL을 열고 DLL 함수를 실행하고 DLL을 닫을 수 있습니다 |
| JSONHandler | JSON Object Manager | 3 | Queries JSON objects and returns strings |
| If | If | 3 | If 및 else 동작입니다. |
| Screen | 화면 | 3 | 애플리케이션 창, 전체 화면 또는 열려 있는 활성 창의 영역을 캡처하는 프로세스를 자동화하고 지정된 위치에 이미지 형식으로 저장합니다. |
| TaskBot | 태스크 봇 | 3 | TaskBot을 실행합니다. |
| Loop | 루프 | 3 | 일련의 동작을 반복합니다. |
| Recorder | 레코더 | 3 | 이 패키지는 객체 작업을 수행하는 데 사용할 수 있습니다. |
| Twilio | Twilio | 2 | Send SMS and make voice calls using Twilio API |
| LogToFile | 파일에 기록 | 2 | 데이터가 있는 로그 파일을 생성합니다. |
| DMNEngine | DMN Engine | 2 | Embedded DMN Engine |
| String_Diff | String_Diff | 2 | Compare two strings. |
| Analyze | 분석... | 2 | 본 패키지는 분석용으로 사용할 수 있습니다. |
| Comment | 코멘트 | 1 | 자동화 태스크 목록에 사용자 지정 코멘트를 추가하여 논리에 대한 추가 정보를 제공합니다. 이 코멘트는 로직 실행 시 무시됩니다. |
| MessageBox | 메시지 상자: | 1 | 메시지 상자를 표시합니다. |
| Step | 단계 | 1 | 단계 작업 |
| SystemVariablesPackage | System Variables Package | 1 | Provides additional system variables related to the bot runner machine which can |
| A2019DemoPackage | CharaCode Converter | 1 | Provides actions to convert character code in file. |
| DictionaryDemo | Dictionary Demo Package | 1 | Sample package to demo dictionary return |
| Text_Diff | Text Diff | 1 | Compare two list data. |
| Math | Math | 1 | Provides Math actions |
| Credential Manager | Credential Manager | 1 | Access Credential Vault values anywhere within your bot |
| FileDetails | A2019DemoPackage | 1 | A2019DemoPackage 작업에 대한 동작을 제공합니다. |
| ReturningAListDemo | Returning A List | 1 | Sample Code for Returning a List |
| Hexadecimal | A2019DemoPackage | 1 | A2019DemoPackage 작업에 대한 동작을 제공합니다. |
| Imagine Loan Approval | A2019DemoPackage | 1 | A2019DemoPackage 작업에 대한 동작을 제공합니다. |
| Application | 애플리케이션 | 1 | 애플리케이션 작업을 수행하기 위한 활동을 제공합니다. |
| Delay | 지연 | 1 | 지연 동작을 수행합니다. |

## 패키지별 액션 상세

### Excel 고급 (`Excel_MS`) — 액션 53개

이전 Excel 형식과 고급 Excel 작업을 지원합니다. MS Excel 프로그램을 설치해야 합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **워크시트를 데이터 테이블로 가져오기** (`getWorksheetAsDataTable`) | 지정된 워크시트에서 데이터 테이블을 생성합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 워크시트 이름 입력:(RADIO*: 활성 워크시트/특정 워크시트), 시트에 헤더 포함(CHECKBOX), Read option(RADIO*: Read visible text in cell/Read cell value), 세션 이름(SESSION*) | TABLE |
| **워크시트 이름 가져오기** (`getWorksheetNames`) | 통합 문서에서 워크시트 이름을 검색합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 세션 이름(SESSION*) | LIST |
| **셀로 이동** (`GoToCell`) | 지정된 셀로 이동합니다. 이 작업은 xlsx, xlsm, xlsb 및 csv 파일에서 작동합니다. | 셀 옵션(RADIO*: 특정 셀/활성 셀), 세션 이름(SESSION*) | - |
| **다음 빈 셀로 이동** (`GoToNextEmptyCell`) | 다음 빈 셀로 이동합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 시작점(RADIO*: 활성 셀/특정 셀), 방향(RADIO*: 왼쪽/오른쪽/위로/아래로), 세션 이름(SESSION*) | - |
| **선택 영역에서 행/열 숨기기** (`HideRowsColumnsInSelection`) | 선택 영역에서 행/열을 숨깁니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | hideRowsSelected(RADIO*: 행 숨기기/열 숨기기), 세션 이름(SESSION*) | - |
| **워크시트 숨기기** (`HideWorksheet`) | 명명된 워크시트를 숨깁니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 숨길 워크시트 이름 입력(TEXT*), 세션 이름(SESSION*) | - |
| **행/열 삽입/삭제** (`InsertDeleteRowColumn`) | 스프레드시트 내에 행/열을 삽입/삭제합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | rowOperationsRequested(RADIO*: 행 작업/열 작업), 세션 이름(SESSION*) | - |
| **테이블 열 삽입** (`InsertTableColumn`) | 스프레드시트에 테이블 열을 삽입합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 테이블 이름(TEXT*), 열 이름(TEXT), 열 위치(NUMBER*), 세션 이름(SESSION*) | - |
| **열기** (`OpenSpreadsheet`) | Excel 스프레드시트를 엽니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 파일 경로(FILE*), 특정 시트 이름(CHECKBOX*), 열기(RADIO*: 읽기 전용 모드/쓰기 전용 모드), 비밀번호가 필요합니다.(CHECKBOX*), 시트에 헤더 포함(CHECKBOX), 추가 기능 로드(CHECKBOX*), 세션 이름(TEXT*) | - |
| **통합 문서 보호** (`ProtectWorkbook`) | 비밀번호로 통합 문서를 보호합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 통합 문서 보호 - 통합 문서를 열려면 비밀번호가 필요합니다.(CHECKBOX*), 통합 문서 구조 보호 - 사용자가 워크시트를 추가, 이동, 삭제, 숨기기, 이름 바꾸기를 하지 못하게 하고, 숨겨진 워크시트를 보지 못하게 합니다.(CHECKBOX*), 세션 이름(SESSION*) | - |
| **비밀번호 보호 워크시트** (`ProtectWorksheet`) | 비밀번호로 워크시트를 보호합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 비밀번호(CREDENTIAL*), 셀 서식(CHECKBOX*), 열 서식(CHECKBOX*), 행 서식(CHECKBOX*), 열 삽입(CHECKBOX*), 행 삽입(CHECKBOX*), 하이퍼링크 삽입(CHECKBOX*), 열 삭제(CHECKBOX*), 행 삭제(CHECKBOX*), 분류(CHECKBOX*), 자동 필터 사용(CHECKBOX*), 피벗 테이블 및 피벗 차트 사용(CHECKBOX*), 객체 편집(CHECKBOX*), 시나리오 편집(CHECKBOX*), 세션 이름(SESSION*) | - |
| **셀 수식 읽기** (`ReadCellFormula`) | 셀 수식을 읽습니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 셀 수식 가져오기(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **열 읽기** (`ReadExcelColumn`) | 열에서 값을 읽습니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀에서/특정 셀에서), 전체 열 읽기(CHECKBOX*), Read option(RADIO*: Read visible text in cell/Read cell value), 세션 이름(SESSION*) | LIST |
| **행 읽기** (`readExcelRow`) | 행에서 값을 읽습니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀에서/특정 셀에서), 전체 행 읽기(CHECKBOX*), Read option(RADIO*: Read visible text in cell/Read cell value), 세션 이름(SESSION*) | LIST |
| **빈 행 제거** (`removeBlankRows`) | 지정된 범위에서 빈 행을 제거합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 시작 행(RADIO*: 시트 시작/특정 행), 마지막 행(RADIO*: 채워진 시트의 끝/특정 행), 세션 이름(SESSION*) | - |
| **워크시트 이름 바꾸기** (`renameWorksheet`) | 특정 워크시트의 이름을 바꿉니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | sheetOption(RADIO*: 워크시트 이름 입력/워크시트 색인 입력), 워크시트의 새 이름 입력(TEXT*), 세션 이름(SESSION*) | - |
| **시트 개수 검색** (`RetrieveSheetsCount`) | 통합 문서에서 워크시트의 수를 검색합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | excludeHiddenWorksheets(RADIO*: 숨겨진 워크시트 제외/숨겨진 워크시트 포함), 세션 이름(SESSION*) | NUMBER |
| **매크로 실행** (`RunMacro`) | Excel 워크시트에서 매크로를 실행합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 매크로 이름(TEXT*), 매크로 인수(TEXT), 세션 이름(SESSION*) | - |
| **통합 문서 저장** (`SaveSpreadSheet`) | Excel 스프레드시트를 저장합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 세션 이름(SESSION*) | - |
| **셀/행/열 선택** (`SelectRowColumnCellRange`) | 사용자가 지정한 대로 셀/행/열을 선택합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 선택(SELECT*: 행/열/셀), 세션 이름(SESSION*) | - |
| **셀 설정** (`SetCell`) | Excel 파일의 셀 값을 설정합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 셀 값(TEXT*), 세션 이름(SESSION*) | - |
| **셀 수식 설정** (`SetCellFormula`) | 지정된 셀의 수식을 설정합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 셀 수식 설정(RADIO*: 활성 셀/특정 셀), 특정 셀의 수식 입력(TEXT*), 세션 이름(SESSION*) | - |
| **Set session variable** (`setSessionVariable`) | Sets the value of a variable so it can be passed to other bots | Session name(TEXT*) | SESSION |
| **테이블 정렬** (`SortTable`) | Excel 시트 내의 테이블을 정렬합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 테이블 이름(TEXT*), 정렬(RADIO*: 열 이름/열 위치), 정렬 순서(RADIO*: 수/텍스트), 세션 이름(SESSION*) | - |
| **시트로 전환** (`SwitchToSheet`) | Excel 파일에서 시트로 전환합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 시트 활성화 기준(RADIO*: 인덱스/이름), 세션 이름(SESSION*) | - |
| **모든 워크시트 숨기기 취소** (`UnhideAllWorksheet`) | Excel 파일에서 모든 워크시트를 숨기기 취소합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 세션 이름(SESSION*) | - |
| **선택 영역에서 행/열 숨기기 취소** (`UnhideRowsColumnsInSelection`) | 선택 영역에서 행/열을 숨기기 취소합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | unhideRowsSelected(RADIO*: 행 숨기기 취소/열 숨기기 취소), 세션 이름(SESSION*) | - |
| **워크시트 숨기기 취소** (`UnhideWorksheet`) | Excel 통합 문서의 지정된 워크시트를 숨기기 취소합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 숨기기 취소할 워크시트 이름 입력(TEXT*), 세션 이름(SESSION*) | - |
| **통합 문서 보호 해제** (`UnprotectWorkbook`) | 비밀번호로 보호된 통합 문서를 보호 해제합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 통합 문서 보호 해제 - 통합 문서를 여는 데 더 이상 비밀번호가 필요하지 않습니다.(CHECKBOX*), 통합 문서 구조 보호 해제 - 사용자가 워크시트를 추가, 이동, 삭제, 숨기기, 이름 바꾸기를 할 수 있고, 숨겨진 워크시트를 볼 수 있게 합니다.(CHECKBOX*), 세션 이름(SESSION*) | - |
| **데이터 테이블로부터 쓰기** (`writeDataTableToWorksheet`) | 데이터 테이블의 내용을 지정된 워크 시트에 씁니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 데이터 테이블 변수 입력(VARIABLE*), 워크시트 이름 입력:(RADIO*: 활성 워크시트/특정 워크시트), 첫 번째 셀 지정(TEXT*), 세션 이름(SESSION*) | - |
| **비밀번호로 보호된 워크시트에 액세스** (`Unprotectworksheet`) | 비밀번호로 보호된 워크시트를 보호 해제합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 비밀번호(CREDENTIAL*), 세션 이름(SESSION*) | - |
| **통합 문서 추가** (`appendWorkbook`) | 지정된 통합 문서의 모든 워크시트를 현재 통합 문서에 추가합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 통합 문서에서 추가(TEXT*), 비밀번호가 필요합니다.(CHECKBOX*), 세션 이름(SESSION*) | - |
| **워크시트 추가** (`appendWorksheet`) | 지정된 통합 문서의 워크시트를 현재 통합 문서에 추가합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 통합 문서에서 추가(FILE*), 비밀번호가 필요합니다.(CHECKBOX*), 통합 문서에서 추가할 워크시트:(RADIO*: 워크시트 이름 입력/워크시트 색인 입력), 세션 이름(SESSION*) | - |
| **닫기** (`CloseSpreadsheet`) | Excel 스프레드시트를 닫습니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 파일을 닫을 때 변경 사항 저장(CHECKBOX*), 세션 이름(SESSION*) | - |
| **Excel을 PDF로 변환** (`ConvertToPDF`) | Excel 통합 문서 또는 워크시트를 PDF 파일로 변환합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 변환할 시트 선택(RADIO*: 전체 Excel 파일/활성 시트/특정 시트), PDF 파일 이름 선택(FILE), PDF 스토리지 위치 선택(TEXT*), 동일한 이름의 PDF가 이미 선택한 위치에 있는 경우(RADIO*: 기존 파일 덮어쓰기/덮어쓰지 않음(예: 파일을 파일 이름 (2)로 저장)), 세션 이름(SESSION*) | - |
| **통합 문서 생성** (`CreateSpreadsheet`) | Excel 통합 문서를 생성합니다. 이 작업은 xlsx, xls, xlsm 및 csv 파일에서 작동합니다. | 파일 경로(FILE*), 시트 이름(TEXT), 열 비밀번호(CREDENTIAL), 편집할 비밀번호(CREDENTIAL), 세션 이름(TEXT*) | - |
| **워크시트 생성** (`CreateWorksheet`) | Excel 워크시트를 생성합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 시트 생성 기준(RADIO*: 인덱스/이름), 세션 이름(SESSION*) | - |
| **셀 삭제** (`DeleteCells`) | Excel 워크시트에서 지정된 셀의 값을 삭제합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 옵션 삭제(RADIO*: 셀을 왼쪽으로 이동/셀을 위로 이동/행 전체/열 전체), 세션 이름(SESSION*) | - |
| **테이블 열 삭제** (`DeleteTableColumn`) | 스프레드시트에서 테이블 열을 삭제합니다. 이 작업은 xlsx 및 xlsm 파일에서 작동합니다. | 테이블 이름(TEXT*), 열 삭제 기준(RADIO*: 이름/위치), 세션 이름(SESSION*) | - |
| **워크시트 삭제** (`DeleteSpreadsheet`) | Excel 워크시트를 삭제합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 시트 삭제(RADIO*: 인덱스/이름), 세션 이름(SESSION*) | - |
| **테이블 필터링** (`FilterTable`) | Excel 시트 내의 테이블을 필터링합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 테이블 이름(TEXT*), filterOption(RADIO*: 열 이름/열 위치), filterBasedOnNumberSelected(RADIO*: 수/텍스트), 세션 이름(SESSION*) | - |
| **찾기** (`find`) | Excel 파일에서 내용을 찾습니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 시작(SELECT*: 시작/End/활성 셀/특정 셀), 까지(SELECT*: 시작/End/활성 셀/특정 셀), 찾기(TEXT*), 검색 옵션(RADIO*: 행별/열별), 대소문자 구분(CHECKBOX*), 전체 셀 내용과 일치(CHECKBOX*), 세션 이름(SESSION*) | LIST |
| **다음 빈 셀 찾기** (`FindNextEmptyCell`) | 다음 빈 셀을 찾습니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 트래버스(RADIO*: 행/열), 시작점(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **셀 색상 가져오기** (`GetCellColor`) | 지정된 셀의 색상을 검색합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | cellColorOption(RADIO*: 배경 색상/텍스트 색상), cellOption(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **현재 워크시트 이름 가져오기** (`GetCurrentWorksheetName`) | 현재 워크시트의 이름을 검색합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 세션 이름(SESSION*) | STRING |
| **여러 셀 가져오기** (`GetMultipleCells`) | Excel 워크시트에서 여러 셀의 값을 검색합니다. 이 작업은 xlsx, xlx, xlsb, xlsm 및 csv 파일에서 작동합니다. | 반환할 셀 범위 선택(SELECT*: 모든 행/특정 행/셀 범위), Read option(RADIO*: Read visible text in cell/Read cell value), 세션 이름(SESSION*) | TABLE |
| **행 개수 가져오기** (`getNumberOfRows`) | 행 수를 검색합니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 워크시트 선택(RADIO*: 인덱스/이름), fetchRowsType(RADIO*: 비어 있지 않은 행/데이터가 있는 총 행), 세션 이름(SESSION*) | NUMBER |
| **단일 셀 가져오기** (`GetSingleCell`) | Excel 워크시트에서 단일 셀의 값을 검색합니다. 이 작업은 xlsx, xlx, xlsb, xlsm 및 csv 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), Read option(RADIO*: Read visible text in cell/Read cell value), 세션 이름(SESSION*) | STRING |
| **셀 주소 가져오기** (`GetSingleCellAddress`) | 활성 또는 사용자 지정 셀의 셀 주소를 반환합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **열 이름 가져오기** (`GetSingleColumnName`) | 사용자 지정 셀의 열 이름을 반환합니다.이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **행 번호 가져오기** (`GetSingleRowNumber`) | 사용자 지정 셀의 행 번호를 반환합니다.이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **테이블 범위 가져오기** (`tableGetRange`) | 테이블의 범위를 검색합니다. 이 작업은 xlsx, xls, xlsb 및 xlsm 파일에서 작동합니다. | 테이블 이름(TEXT*), 헤더 포함(CHECKBOX*), 피벗 테이블(CHECKBOX*), 세션 이름(SESSION*) | STRING |
| **바꾸기** (`Replace`) | Excel 파일의 내용을 바꿉니다. 이 작업은 xlsx, xls, xlsb, xlsm 및 csv 파일에서 작동합니다. | 시작(SELECT*: 시작/End/활성 셀/특정 셀), 까지(SELECT*: 시작/End/활성 셀/특정 셀), 찾기(TEXT*), 검색 옵션(RADIO*: 행별/열별), 대소문자 구분(CHECKBOX*), 전체 셀 내용과 일치(CHECKBOX*), 로 바꾸기(TEXT), 세션 이름(SESSION*) | - |

### Web Automation (`WebAutomation`) — 액션 50개

Provides commands for Browser Automation actions.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Check** (`checkelement`) | Check on an Element | Session name(TEXT*), JS Script(TEXTAREA*), Check(BOOLEAN*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Click** (`clickelement`) | Click on an Element | Session name(TEXT*), JS Script(TEXTAREA*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Focus** (`focuselement`) | Focus on an Element | Session name(TEXT*), JS Script(TEXTAREA*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Select** (`selectelement`) | Select an Element Option | Session name(TEXT*), JS Script(TEXTAREA*), Value(TEXT*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **End session** (`EndSessionWebAutomation`) | End session | Session name(TEXT*) | - |
| **Execute JavaScript** (`executejs`) | Execute JavaScript | Session name(TEXT*), JavaScript Code(TEXTAREA*) | STRING |
| **Get Current Session** (`getcurrentsession`) | Get Current Session ID | Session name(TEXT*) | STRING |
| **Get Current URL** (`getcurrenturl`) | Get Current URL | Session name(TEXT*) | STRING |
| **Get Page Source** (`pagesource`) | Get Page Source of a Page | Session name(TEXT*) | STRING |
| **Get Text** (`gettextelement`) | Get Textof an element | Session name(TEXT*), JS Script(TEXTAREA*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | STRING |
| **Get Value** (`getvalueelement`) | Get Value of an element | Session name(TEXT*), JS Script(TEXTAREA*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | STRING |
| **Is Page Loaded** (`isloaded`) | Is Page Loaded | Session name(TEXT*) | BOOLEAN |
| **Open Page** (`openpage`) | Open Page | Session name(TEXT*), URL(TEXT*) | - |
| **Set Value** (`setvalueelement`) | Set Value of an element | Session name(TEXT*), JS Script(TEXTAREA*), Value(CREDENTIAL*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Start session** (`StartSessionWebAutomation`) | Start new session | Session name(TEXT*), Headless(BOOLEAN*), ChromeDriver path(FILE), Existing Remote Session Port(NUMBER), Function Library(TEXTAREA) | - |
| **Wait Element Loaded** (`elementloaded`) | Wait until Element is loaded | Session name(TEXT*), Base JS Script(TEXTAREA*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | BOOLEAN |
| **Wait Page Loaded** (`pageloaded`) | Wait until Page is loaded | Session name(TEXT*), Timeout (Seconds)(NUMBER*) | BOOLEAN |
| **Clear Input** (`clearinput`) | Clears Input for an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Check** (`checkelement`) | Check on an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Check(BOOLEAN*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Click** (`clickelement`) | Click on an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Click Type(SELECT*: Click/Right Click (No JS)/Double Click (No JS)), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Click and Hold** (`clicknhold`) | Click and Hold an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Focus** (`focuselement`) | Focus on an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Release** (`release`) | Release mouse button | Session name(TEXT*) | - |
| **Select** (`selectelement`) | Select an Element Option | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Value(TEXT*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Submit** (`submitelement`) | Submit a Form | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Drag an Drop** (`dragdrop`) | Drag and Drop an Element | Session name(TEXT*), From Element(TEXTAREA*), To Element(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **End session** (`EndSessionWebAutomation`) | End session | Session name(TEXT*), Close Browser(BOOLEAN*) | - |
| **Execute JavaScript** (`executejs`) | Execute JavaScript | Session name(TEXT*), JavaScript Code(TEXTAREA*) | STRING |
| **Get Values** (`getvalueselement`) | Get Values of an element list | Session name(TEXT*), Searches(LIST*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript) | DICTIONARY |
| **Get Current Session** (`getcurrentsession`) | Get Current Session ID | Session name(TEXT*) | STRING |
| **Get Current URL** (`getcurrenturl`) | Get Current URL | Session name(TEXT*) | STRING |
| **Get Current Window** (`getcurrentwindow`) | Get Current Window | Session name(TEXT*) | STRING |
| **Element Details** (`elementdetails`) | Get Element Details | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Filename to store element screenshot as PNG image(TEXT), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | DICTIONARY |
| **Get Page Source** (`pagesource`) | Get Page Source of a Page | Session name(TEXT*) | STRING |
| **Get Table Content** (`gettablecontent`) | Get Table Content | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | TABLE |
| **Get Text** (`gettextelement`) | Get Text of an element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | STRING |
| **Get Value** (`getvalueelement`) | Get Value of an element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | STRING |
| **Get Windows** (`getwindows`) | Get Windows | Session name(TEXT*) | LIST |
| **Is Page Loaded** (`isloaded`) | Is Page Loaded | Session name(TEXT*) | BOOLEAN |
| **Move to** (`movetoelement`) | Move to an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Open Page** (`openpage`) | Open Page | Session name(TEXT*), URL(TEXT*) | - |
| **Select Frame** (`selectframe`) | Select Frame | Session name(TEXT*), Frame Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Select Window** (`selectwindow`) | Select Window | Session name(TEXT*), Window Handle(TEXT*) | - |
| **Send Key Strokes** (`sendkeys`) | Send Keys to an Element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector), Keys(SELECT*: Keys/Credential), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Set Values** (`setvalueselement`) | Set Values of an element list | Session name(TEXT*), Searches(LIST*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Values(LIST*) | - |
| **Set Value** (`setvalueelement`) | Set Value of an element | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Value(CREDENTIAL*), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | - |
| **Start Session** (`StartSessionWebAutomation`) | Start New Session | Session name(TEXT*), Browser(SELECT*: Chrome/Edge), Window Width(NUMBER*), Window Height(NUMBER*), Headless(BOOLEAN*), WebDriver path(FILE), User Profile path(TEXT), Existing Remote Session Port(NUMBER), Function Library(TEXTAREA) | - |
| **To Default** (`todefaultcontent`) | Reset to Default Content (if set to a frame before) | Session name(TEXT*) | - |
| **Wait Element Loaded** (`elementloaded`) | Wait until Element is loaded | Session name(TEXT*), Search(TEXTAREA*), Search Type(SELECT*: Search by Element XPath/Search by Element Id/Search by Tag name/Search by CSS Selector/JavaScript), Timeout (Seconds)(NUMBER*), Wait for Attribute Value(TEXT*) | BOOLEAN |
| **Wait Page Loaded** (`pageloaded`) | Wait until Page is loaded | Session name(TEXT*), Timeout (Seconds)(NUMBER*) | BOOLEAN |

### 문자열 (`String`) — 액션 16개

문자열 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **지정** (`assign`) | 주어진 문자열 지정 또는 연결 | 소스 문자열 변수/값 선택(TEXT) | STRING |
| **텍스트 추출** (`beforeAfter`) | ‘Before' 및 'After'로 지정된 두 개의 주어진 문자열 사이의 하위 문자열을 추출합니다. | 소스 문자열(TEXTAREA*), 문자 가져오기(RADIO*: 전/전 및/또는 후/후), 일치하는 항목이 없으면 돌아가기(RADIO*: 소스 문자열/빈(Null) 문자열), 가져올 문자의 수(RADIO*: 전체/만), 추출된 텍스트 자르기(공백 제거)(CHECKBOX), 추출된 텍스트로부터 엔터 제거(CHECKBOX) | STRING |
| **비교** (`compare`) | 두 개의 문자열을 비교하고 문자열이 동일한 경우 참을 반환합니다. | 소스 문자열(TEXT*), 비교 문자열(TEXT*), 비교 시(RADIO*: 대소문자 구분/대소문자 구분하지 않음) | BOOLEAN |
| **찾기** (`find`) | 소스 문자열 내에서 주어진 문자열 위치를 찾습니다. | 소스 문자열(TEXT*), 문자열 찾기:(TEXT*), 찾기 시(RADIO*: 대소문자 구분/대소문자 구분하지 않음), "find string"은(RADIO*: 정규식/정규식이 아님), 시작점(NUMBER) | NUMBER |
| **길이:** (`length`) | 문자열의 길이를 가져옵니다. | 소스 문자열(TEXT*) | NUMBER |
| **소문자** (`lowercase`) | 소스 문자열을 소문자로 변환합니다. | 소스 문자열(TEXT*) | STRING |
| **랜덤 문자열 생성** (`randomString`) | 문자열 변수에 랜덤 문자열을 할당합니다. | 문자열 길이(NUMBER*) | STRING |
| **문자열을** (`ImportStringFromTextFile`) | 텍스트 파일에서 값을 문자열로 가져오기합니다. | 텍스트 파일에서 값을 문자열로 가져오기합니다.(FILE*), 텍스트 파일 내 변수 키(TEXT*) | STRING |
| **바꾸기** (`replace`) | '문자열 바꾸기'를 통해 '소스 문자열' 중 지정 일부 대체 | 소스 문자열(TEXT*), 문자열 찾기:(TEXT*), 찾기 시(RADIO*: 대소문자 구분/대소문자 구분하지 않음), "find string"은(RADIO*: 정규식/정규식이 아님), 시작점(NUMBER), 개수(NUMBER), 로 바꾸기(TEXT) | STRING |
| **반대로 뒤집기** (`reverse`) | 소스 문자열을 반대로 뒤집습니다. | 소스 문자열(TEXT*) | STRING |
| **분할** (`split`) | 구분 기호를 사용하여 소스 문자열을 여러 문자열로 분할합니다. | 소스 문자열(TEXT*), 구분 기호:(TEXT*), 구분 기호는(RADIO*: 대소문자 구분/대소문자 구분하지 않음), 하위 문자열로 분할(RADIO*: 모두 가능/만) | LIST |
| **하위 문자열** (`subString`) | 주어진 문자열로부터 하위 문자열을 추출합니다. | 소스 문자열(TEXT*), 시작점(NUMBER*), 길이:(NUMBER) | STRING |
| **부울로** (`toBoolean`) | 문자열 값을 부울로 변환하고 부울 변수에 지정합니다. | 문자열 변수 선택(VARIABLE*) | BOOLEAN |
| **숫자로** (`toNumber`) | 문자열을 숫자로 변환 | 문자열 입력(TEXT*) | NUMBER |
| **자르기** (`trim`) | 주어진 문자열로부터 공백을 자릅니다. | 소스 문자열(TEXT*), 시작부터 자르기(CHECKBOX), 끝부터 자르기(CHECKBOX) | STRING |
| **대문자로 만듭니다** (`uppercase`) | 소스 문자열을 대문자로 변환합니다. | 소스 문자열(TEXT*) | STRING |

### Excel 기본 (`Excel`) — 액션 15개

MS Excel 프로그램을 설치할 필요 없이 .xlsx 파일에 대한 빠른 스프레드시트 작업이 필요합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **닫기** (`CloseSpreadsheet`) | Excel 스프레드시트 닫기 | 파일을 닫을 때 변경 사항 저장(CHECKBOX*), 세션 이름(SESSION*) | - |
| **셀 삭제** (`DeleteCells`) | Excel 스프레드시트에서 주어진 셀의 값 삭제 | 셀 옵션(RADIO*: 활성 셀/특정 셀), 옵션 삭제(RADIO*: 셀을 왼쪽으로 이동/셀을 위로 이동/행 전체/열 전체), 세션 이름(SESSION*) | - |
| **찾기** (`find`) | Excel 파일에서 내용을 찾습니다. | 시작(SELECT*: 시작/End/활성 셀/특정 셀), 까지(SELECT*: 시작/End/활성 셀/특정 셀), 찾기(TEXT*), 검색 옵션(RADIO*: 행별/열별), 대소문자 구분(CHECKBOX*), 전체 셀 내용과 일치(CHECKBOX*), 세션 이름(SESSION*) | LIST |
| **여러 셀 가져오기** (`GetMultipleCells`) | Excel 스프레드시트에서 여러 셀의 값을 검색합니다. | 반복(SELECT*: 모든 행/특정 행/셀 범위), 세션 이름(SESSION*) | TABLE |
| **단일 셀 가져오기** (`GetSingleCell`) | Excel 스프레드시트에서 셀 값 검색 | 셀 옵션(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **셀 주소 가져오기** (`GetSingleCellAddress`) | 활성 셀의 셀 주소를 검색합니다. | 세션 이름(SESSION*) | STRING |
| **열 이름 가져오기** (`GetSingleColumnName`) | 사용자 지정 셀의 열 이름을 반환합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **행 번호 가져오기** (`GetSingleRowNumber`) | 사용자 지정 셀의 행 번호를 반환합니다. | 셀 옵션(RADIO*: 활성 셀/특정 셀), 세션 이름(SESSION*) | STRING |
| **셀로 이동** (`GoToCell`) | Excel 스프레드시트에서 특정 셀로 이동 | 셀 옵션(RADIO*: 특정 셀/활성 셀), 세션 이름(SESSION*) | - |
| **열기** (`OpenSpreadsheet`) | Excel 스프레드시트 열기 | 파일 경로(FILE*), 특정 시트 이름(CHECKBOX*), 열기(RADIO*: 읽기 전용 모드/쓰기 전용 모드), 비밀번호가 필요합니다.(CHECKBOX*), 시트에 헤더 포함(CHECKBOX), 세션 이름(TEXT*) | - |
| **바꾸기** (`Replace`) | Excel 파일의 내용 바꾸기 | 시작(SELECT*: 시작/End/활성 셀/특정 셀), 까지(SELECT*: 시작/End/활성 셀/특정 셀), 찾기(TEXT*), 검색 옵션(RADIO*: 행별/열별), 대소문자 구분(CHECKBOX*), 전체 셀 내용과 일치(CHECKBOX*), 로 바꾸기(CHECKBOX*), 세션 이름(SESSION*) | - |
| **통합 문서 저장** (`SaveSpreadSheet`) | Excel 스프레드시트 저장 | 세션 이름(SESSION*) | - |
| **셀 설정** (`SetCell`) | Excel 스프레드시트에서 주어진 셀에 값 설정 | 사용(RADIO*: 활성 셀/특정 셀), 설정할 값(TEXT*), 세션 이름(SESSION*) | - |
| **Set session variable** (`setSessionVariable`) | Sets the value of a variable so it can be passed to other bots | Session name(TEXT*) | SESSION |
| **시트로 전환** (`ActivateSheet`) | Excel 파일에서 시트로 전환 | 시트 활성화 기준(RADIO*: 인덱스/이름), 세션 이름(SESSION*) | - |

### Trello (`Trello`) — 액션 14개

Provides actions to integrate with Trello

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Add Attachment to Card** (`Add Attachment to Card`) | Attaches a file to a card | Session name(TEXT*), Board Name(TEXT*), Card Name(TEXT*), Attachment Details(GROUP) | STRING |
| **Add Comment to Card** (`Add Comment to Card`) | Adds a comment to a card | Session name(TEXT*), Board Name(TEXT*), Card Name(TEXT*), Comment(TEXTAREA*) | STRING |
| **Add Label** (`Add label to card`) | Adds label to card | Session name(TEXT*), Board Name(TEXT*), Card Name(TEXT*), Label Name(TEXT*) | - |
| **Manage Members** (`Manage Members on Board or Team`) | Manage members on board or team in Trello | Session name(TEXT*), Action(RADIO*: Add/Remove), Members to or from(RADIO*: Team/Board), Board or Team Name(TEXT*), Member User Names(TEXTAREA*) | - |
| **Create Board** (`Create Board`) | Creates a new board in Trello | Session name(TEXT*), Board Details(GROUP), Team(TEXT), Background Color(SELECT*: Blue/Red/Pink/Orange/Green/Purple), Permission Level(SELECT*: Organization/Team/Private/Public), Voting(SELECT*: Disabled/Members/Observers/Organization/Public), Comments(SELECT*: Disabled/Members/Observers/Organization/Public), Invitations(SELECT*: Admins/Members) | STRING |
| **Create Card** (`Create Card`) | Creates a new card on a list | Session name(TEXT*), Board Name(TEXT*), List Name(TEXT*), Card Details(GROUP), Position(RADIO*: Top/Bottom), Due Date(TEXT) | STRING |
| **Create Label** (`Create Label on Board`) | Creates label on board | Session name(TEXT*), Board Name(TEXT*), Label Name(TEXT*), Label Color(SELECT*: Blue/Red/Pink/Orange/Green/Purple) | STRING |
| **Create List** (`Create List`) | Creates a new list in Trello | Session name(TEXT*), Board Name(TEXT*), List Name(TEXT*) | STRING |
| **Create Team** (`Create Team`) | Creates a new team in Trello | Session name(TEXT*), Team Details(GROUP) | STRING |
| **Delete Board** (`Delete Board`) | Deletes a board in Trello | Session name(TEXT*), Board Name(TEXT*) | - |
| **Delete Card** (`Delete Card`) | Deletes a card in Trello | Session name(TEXT*), Board Name(TEXT*), Card Name(TEXT*) | - |
| **Delete Team** (`Delete Team`) | Deletes a team in Trello | Session name(TEXT*), Team Name(TEXT*) | - |
| **End Session** (`End Session`) | Session End | Session name(TEXT*) | - |
| **Start Session** (`Start Session`) | Enter API Key and Token for Trello Account | Session name(TEXT*), API Key(CREDENTIAL*), Account Token(CREDENTIAL*) | - |

### 이메일 (`Email`) — 액션 13개

이메일 작업을 수행하기 위한 활동을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **상태 변경** (`changeStatus`) | 이메일 상태를 읽음/읽지 않음으로 변경 루프 안에서 이 동작 사용 | 세션 이름(TEXT*), 상태 변경(RADIO*: 읽기/읽지 않음) | - |
| **폴더가 존재하는지 확인** (`checkFolder`) | 주어진 이름의 폴더가 존재하는지 확인하고 결과(참/거짓)를 부울 변수로 반환 | 세션 이름(TEXT*), 폴더 이름(TEXT*) | BOOLEAN |
| **모두 삭제** (`deleteAll`) | 모든 이메일 메시지를 삭제합니다. | 세션 이름(TEXT*), 삭제할 이메일의 유형(RADIO*: 전체/읽기/읽지 않음) | - |
| **삭제** (`deleteMessage`) | 단일 이메일을 삭제합니다. 루프 안에서 이 작업을 사용합니다. | 세션 이름(TEXT*) | - |
| **연결 끊기** (`closeEmail`) | 이메일 서버와의 연결 닫기 | 세션 이름(TEXT*) | - |
| **연결 ** (`emailConnect`) | 이메일 서버에 연결 | 세션 이름(TEXT*), 연결(RADIO*: Outlook/이메일 서버/EWS 서버) | - |
| **전달** (`ForwardEmail`) | 동일한 제목의 이메일을 전달합니다. 루프 안에서 이 작업을 사용합니다. | To 주소(TEXT*), Cc(TEXT), Bcc(TEXT), 첨부(FILE), 첨부파일이 없는지 확인(CHECKBOX*), 이메일 보내기(RADIO*: 일반 텍스트로/HTML), 메시지(TEXTAREA), 이메일 끝에 고그린(Go Green) 메시지 포함(CHECKBOX*), 이메일 전송 경로(SELECT*: 이메일 서버/Outlook/EWS 서버) | - |
| **모두 이동** (`moveEmail`) | 모든 이메일 메시지를 지정된 폴더로 이동합니다 | 세션 이름(TEXT*), 사서함의 대상 폴더 경로(TEXT*), 이동할 이메일 유형(RADIO*: 전체/읽음/읽지 않음), 특정 폴더로부터(TEXT*), 제목에 포함되는 경우(TEXT), 특정 발신자로부터(TEXT), 수신 날짜 또는 이후인 경우(VARIABLE), 수신 날짜가 전인 경우(VARIABLE) | - |
| **회신** (`ReplyEmail`) | 동일한 제목으로 이메일 발신자에게 회신합니다. 루프 안에서 이 작업을 사용합니다. | Cc(TEXT), Bcc(TEXT), 첨부(FILE), 첨부파일이 없는지 확인(CHECKBOX*), 이메일 보내기(RADIO*: 일반 텍스트로/HTML), 메시지(TEXTAREA), 이메일 끝에 고그린(Go Green) 메시지 포함(CHECKBOX*), 이메일 전송 경로(SELECT*: 이메일 서버/Outlook/EWS 서버) | - |
| **모든 첨부파일 저장** (`saveAllAtatchments`) | 특정 서버로부터 여러 이메일의 모든 첨부파일 저장 | 세션 이름(TEXT*), 가져올 이메일의 유형(RADIO*: 전체/읽기/읽지 않음), 첨부파일을 폴더에 저장(TEXT*), 파일 덮어쓰기(CHECKBOX*) | - |
| **첨부파일 저장** (`saveAttachment`) | 단일 이메일의 모든 첨부파일을 저장합니다. 루프 안에서 이 작업을 사용합니다. | 첨부파일을 폴더에 저장(TEXT*), 파일 덮어쓰기(CHECKBOX*) | - |
| **이메일 저장** (`saveEmail`) | 단일 이메일을 저장합니다. 루프 안에서 이 동작 사용 | 세션 이름(TEXT*), 폴더에 이메일 저장(TEXT*), 파일 덮어쓰기(CHECKBOX*) | - |
| **보내기** (`sendMail`) | 이메일 전송 | To 주소(TEXT*), Cc(TEXT), Bcc(TEXT), , 제목:(TEXT*), 첨부(FILE), 첨부파일이 없는지 확인(CHECKBOX*), 이메일 보내기(RADIO*: 일반 텍스트로/HTML), 메시지(TEXTAREA*), 이메일 끝에 고그린(Go Green) 메시지 포함(CHECKBOX*), 이메일 전송 경로(SELECT*: 이메일 서버/Outlook/EWS 서버) | - |

### AWS DynamoDB (`AWSDynamoDB`) — 액션 11개

Provides actions for AWS DynamoDB operations.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Create Table** (`CreateTable`) | Create a Table | Session Name(TEXT*), Table(TEXT*), Partition Key(TEXT*), Partition Key Type(SELECT*: String/Number), Sort Key(TEXT), Sort Key Type(SELECT: String/Number), Throughput(NUMBER*) | STRING |
| **Delete Item** (`DeleteItem`) | Delete Item | Session Name(TEXT*), Table(TEXT*), Partition Key(TEXT*), Partition Value(VARIABLE*), Sort Key(TEXT), Sort Key Value(VARIABLE) | STRING |
| **Delete Table** (`DeleteTable`) | Delete a Table | Session Name(TEXT*), Table(TEXT*) | STRING |
| **End Session** (`EndDYNDBSession`) | End DynamoDB Session | Session Name(TEXT*) | - |
| **Get Attribute Value** (`GetAttributeValue`) | Get Attribute Value of a Item | Session Name(TEXT*), Item(TEXT*), JSON Key(TEXT*) | STRING |
| **Get Item** (`GetItem`) | Get Item | Session Name(TEXT*), Table(TEXT*), Partition Key(TEXT*), Partition Value(VARIABLE*), Sort Key(TEXT), Sort Key Value(VARIABLE) | STRING |
| **Get List of Tables** (`GetListTables`) | Get the DynamoDB tables | Session Name(TEXT*) | LIST |
| **Insert Item** (`InsertItem`) | Insert Item | Session Name(TEXT*), Table(TEXT*), Partition Key(TEXT*), Partition Value(VARIABLE*), Dictionary of Item Attributes(DICTIONARY*) | STRING |
| **Scan Table** (`ScanTable`) | Scan Table for Values | Session Name(TEXT*), Table(TEXT*), Dictionary of filter(DICTIONARY*), use AND operator(BOOLEAN*) | LIST |
| **Start Session** (`StartDYNDBSession`) | Start DynamoDB session | Session Name(TEXT*), Region(SELECT*: US East (Ohio)/US East (N. Virginia)/US West (N. California/US West (Oregon)/Asia Pacific (Hong Kong)/Asia Pacific (Mumbai)…), Access Key(CREDENTIAL*), Secret Key(CREDENTIAL*) | - |
| **Update Item** (`UpdateItem`) | Update Item Values | Session Name(TEXT*), Table(TEXT*), Partition Key(TEXT*), Partition Value(VARIABLE*), Sort Key(TEXT), Sort Value(VARIABLE), Dictionary of Item Attributes(DICTIONARY*) | STRING |

### Slack (`Slack`) — 액션 11개

Provides actions to integrate with Slack

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Archive Channel** (`Archive Channel`) | Archive Channel in Slack | Session name(TEXT*), Channel ID(TEXT*) | STRING |
| **Create Channel** (`Create Channel in Slack`) | Creates a channel in Slack | Session name(TEXT*), Channel Name(TEXT*) | DICTIONARY |
| **End Session** (`End Session`) | Session End | Session name(TEXT*) | - |
| **Get Channel ID** (`Returns Channel ID`) | Returns Slack Channel ID based on name | Session name(TEXT*), Channel Name(TEXT*) | STRING |
| **Get Messages** (`Get Messages from Channel`) | Retrieves messages from a channel in Slack | Session name(TEXT*), Channel ID(TEXT*) | TABLE |
| **Get User ID** (`Returns User ID`) | Returns Slack User ID based on name | Session name(TEXT*), User Name(TEXT*) | STRING |
| **Invite User** (`Invite User to Channel`) | Invite User to Channel in Slack | Session name(TEXT*), Channel ID(TEXT*), User ID(TEXT*) | STRING |
| **Leave Channel** (`Leave Channel`) | Remove Integration from Channel in Slack | Session name(TEXT*), Channel ID(TEXT*) | STRING |
| **Post File** (`Post File`) | Posts a file to one or more Slack channels | Session name(TEXT*), Channel ID(TEXT*), File to Upload(FILE*), Title of File(TEXT) | STRING |
| **Post Message** (`Post Message to Channel`) | Posts a message to a channel in Slack | Session name(TEXT*), Channel Name or ID(TEXT*), Text for message(TEXTAREA*), Message Timestamp for Reply(TEXT) | STRING |
| **Start Session** (`Start Session`) | Enter OAuth token to authenticate with Slack | Session name(TEXT*), Token(CREDENTIAL*) | - |

### 파일 (`File`) — 액션 10개

파일 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **지정** (`assign`) | 사용자 지정 파일 값 또는 소스 파일 변수의 값을 대상 파일 변수에 할당합니다 | 할당할 파일 변수 또는 값(FILE*) | FILE |
| ** 복사...** (`copyFiles`) | 파일 복사 | 소스 파일(FILE2*), 대상 파일/폴더(TEXT*), 기존 파일 덮어쓰기(CHECKBOX*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **생성** (`createFile`) | 파일 생성 | 파일(FILE*), 기존 파일 덮어쓰기(CHECKBOX*) | - |
| **삭제** (`deleteFiles`) | 파일 삭제 | 파일(FILE2*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **CR 파일 다운로드** (`downloadTo`) | Control Room 파일을 특정 위치로 다운로드합니다. | Control Room 파일 선택(FILE*), CR 파일을 위치에 저장(FILE*), 기존 파일 덮어쓰기(BOOLEAN*) | - |
| **열기** (`openFile`) | 파일 열기 | 파일(FILE*) | - |
| **인쇄** (`printFile`) | 지정된 파일 인쇄 | 파일(FILE*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **여러 파일 인쇄** (`printMultipleFiles`) | 여러 파일 인쇄 | 폴더(TEXT*), 파일 유형(SELECT*: String/Regex), 하위 폴더 포함(CHECKBOX), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **이름 바꾸기** (`renameFiles`) | 파일 이름 바꾸기 | 파일(FILE2*), 새 파일 이름(TEXT*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **바로가기 생성** (`createFileShortcut`) | 다른 사용자가 | 소스 파일(FILE*), 대상 폴더(FILE*) | - |

### XML: (`XML`) — 액션 10개

xml 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **노드 삭제** (`deleteNode`) | xml로부터 특정 노드 삭제 | 세션 이름(TEXT*), XPath 식(TEXT*), 속성(TEXT) | - |
| **세션 종료** (`endSession`) | XML 세션 종료 | 세션 이름(TEXT*) | - |
| **XPath 기능 실행** (`executeXPath`) | XML에서 XPath 기능 실행 | 세션 이름(TEXT*), XPath 식(TEXT*) | STRING |
| **여러 노드 가져오기** (`getMultipleNode`) | 여러 xml 노드에서 값 가져오기 | 세션 이름(TEXT*), XPath 식(TEXT*), 각 노드 가져 오기(RADIO*: 텍스트 값/XPath 식/특정 속성 이름) | - |
| **단일 노드 가져오기** (`getSingleNode`) | xml로부터 특정 노드 값 가져오기 | 세션 이름(TEXT*), XPath 식(TEXT*), 속성(TEXT) | STRING |
| **노드 삽입** (`insertNode`) | xml 내에 노드 삽입 | 세션 이름(TEXT*), XPath 식(TEXT*), 노드 이름(TEXT*), 노드 값(TEXTAREA), 노드 이름이 존재하면(SELECT*: 무조건 삽입/건너뛰기/덮어쓰기), 노드 위치 삽입(SELECT*: 하위 노드의 시작/하위 노드의 끝/특정 하위 노드 전/특정 하위 노드 후), 기본 네임스페이스(TEXT), 속성()(DICTIONARY), 네임스페이스(DICTIONARY) | - |
| **세션 데이터 저장** (`saveXML`) | XML 세션 데이터 저장 | 세션 이름(TEXT*), XML 데이터 쓰기(CHECKBOX*) | STRING |
| **세션 시작** (`startSession`) | xml 세션 시작 | 세션 이름(TEXT*), 데이터 소스(RADIO*: 파일/텍스트) | - |
| **노드 업데이트** (`updateNode`) | xml에서 특정 노드 업데이트 | 세션 이름(TEXT*), XPath 식(TEXT*), 새 값(TEXT*), 속성 업데이트(CHECKBOX*) | - |
| **XML 문서 유효성 검사** (`validateXML`) | XML 문서 유효성 검사 | 세션 이름(TEXT*), 변수 유형 선택(RADIO*: XML 스키마(.xsd)/내부 DTD/잘 형성됨) | STRING |

### 목록 (`List`) — 액션 9개

목록 데이터 유형의 변수에 대해 다양한 작업을 수행합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **항목 추가** (`addItem`) | 목록 변수에 항목을 삽입합니다. | 목록 변수(VARIABLE*), 추가할 항목(VARIABLE*), 항목 추가(RADIO*: 목록의 끝까지/목록 색인에서) | - |
| **지정** (`assign`) | 소스 목록의 값을 대상 목록 변수에 할당합니다. | 소스 목록 변수 선택(LIST*) | LIST |
| **지우기** (`clear`) | 선택한 목록 변수에서 모든 항목을 지웁니다. | 목록 변수(VARIABLE*) | - |
| **항목 가져오기** (`get`) | 목록의 지정된 위치에서 값을 검색하고 출력을 변수에 저장합니다. | 목록 변수(VARIABLE*), 인덱스 번호(NUMBER*) | ANY |
| **항목 결합** (`joinList`) | 사용 가능한 모든 값을 목록 변수에 결합하고 출력을 문자열 변수에 저장합니다. | 목록 변수(VARIABLE*), 구분 기호(TEXT) | STRING |
| **추가** (`assignToDataTable`) | 데이터 테이블의 열 인덱스에 목록 변수를 추가합니다. | 목록 변수 선택(VARIABLE*), 데이터 테이블에 결과 할당(VARIABLE*), 인덱스에 열 삽입(RADIO*: 첫 인덱스/마지막 인덱스/특정 인덱스) | - |
| **항목 제거** (`listRemove`) | 목록에서 항목을 제거하고 출력을 변수에 할당합니다. | 목록 변수(VARIABLE*), 인덱스 번호(NUMBER*) | ANY |
| **항목 설정** (`listSet`) | 목록의 특정 위치에 항목을 설정하고 출력을 변수에 저장합니다. | 목록 변수(VARIABLE*), 인덱스 번호(NUMBER*), 로 바꾸기(VARIABLE*) | ANY |
| **크기** (`listSize`) | 목록의 항목 수를 검색하고 출력을 숫자 변수에 할당합니다. | 목록 변수(VARIABLE*) | NUMBER |

### 폴더 (`Folder`) — 액션 8개

폴더 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **압축** (`zipFiles`) | 지정된 파일/폴더를 zip 파일로 압축 | 압축할 파일/폴더 지정(TEXT*), 압축할 유형 지정(SELECT*: String/Regex), 대상 파일 이름 및 위치 지정(FILE*), 최신 버전인 경우에만 업데이트(CHECKBOX), 원본 파일 삭제(CHECKBOX), 압축:(SELECT*: 일반/Fast/Superfast), 비밀번호 보호(CREDENTIAL) | - |
| ** 복사...** (`copyFolder`) | 폴더 복사 | 소스 폴더:(TEXT*), 대상 폴더(TEXT*), 기존 파일/폴더 덮어쓰기(CHECKBOX*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **생성** (`createFolder`) | 폴더 생성 | 폴더(TEXT*), 기존 폴더 덮어쓰기(CHECKBOX*) | - |
| **압축 해제** (`unzipFiles`) | zip 파일의 내용을 지정된 위치에 추출 | 전체 경로가 포함된 Zip 파일 이름(FILE*), 경로에 추출(TEXT*), 기존 파일 바꾸기(CHECKBOX), Zip 파일에 액세스하기 위한 비밀번호(CREDENTIAL) | - |
| **삭제** (`deleteFolder`) | 폴더 삭제 | 폴더(TEXT*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |
| **바로가기 생성** (`createFolderShortcut`) | 다른 사용자가 | 소스 폴더:(FILE*), 대상 폴더(FILE*) | - |
| **열기** (`openFolder`) | 폴더 열기 | 폴더(TEXT*) | - |
| **이름 바꾸기** (`renameFolder`) | 폴더 이름 바꾸기 | 폴더(TEXT*), 새 폴더 이름(TEXT*), 크기(CHECKBOX), 날짜(CHECKBOX) | - |

### 브라우저 (`Browser`) — 액션 8개

브라우저 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **닫기** (`close`) | 브라우저 창이나 탭을 닫습니다 | 닫기 대상(SELECT*: 탭/창/모든 브라우저), 시간 초과 기준(초)(NUMBER) | - |
| **파일 다운로드** (`downloadFile`) | 웹에서 파일 다운로드 | 파일 URL(TEXT*), 위치에 저장(FILE*), 기존 파일 덮어쓰기(CHECKBOX*) | - |
| **소스 코드 받기** (`Extractsource`) | 웹 페이지의 소스 코드를 캡처합니다 | 브라우저 탭(WINDOW*), 시간 초과 기준(초)(NUMBER) | STRING |
| **깨진 링크 찾기** (`findbrokenLinks`) | 웹사이트로부터 유효하지 않은 링크 목록 내보내기 | 페이지 또는 URL(TEXT*), 범위(RADIO*: 이 페이지에서만 확인/전체 사이트 확인), 위치에 목록 저장(FILE*), 이미 존재하는 csv 파일에 추가(CHECKBOX), 인코딩(SELECT*: ANSI/UTF-8/Unicode), 병렬 스레드의 수(NUMBER*), 시간 초과(초)(NUMBER*) | - |
| **뒤로 이동** (`Goback`) | 이전 방문한 웹 페이지로 돌아갑니다 | 브라우저 탭(WINDOW*), 돌아갈 단계의 개수(NUMBER*), 단계가 이력을 초과하면 오류를 표시합니다(CHECKBOX), 시간 초과 기준(초)(NUMBER) | - |
| **웹사이트 열기(레거시)** (`launchWebsite`) | 이 명령은 브라우저에서 웹사이트를 여는 데 사용할 수 있음 | 열 링크(TEXT*), 브라우저(SELECT*: 기본 브라우저/Internet Explorer/Mozilla Firefox/Google Chrome/Microsoft Edge) | - |
| **열기** (`openbrowser`) | 이 명령은 브라우저에서 웹사이트를 여는 데 사용할 수 있음 | 열기 대상(SELECT*: 기존 탭/새 탭/새 창), 열 링크(TEXT*), 시간 초과 기준(초)(NUMBER) | - |
| **JavaScript 실행** (`RunJavaScript`) | 웹 페이지에서 JavaScript를 실행합니다 | 브라우저 탭(WINDOW*), 실행할 JavaScript(RADIO*: 기존 파일 가져오기/수동 입력), 시간 초과 기준(초)(NUMBER) | ANY |

### A2019DemoPackage (`Twitter`) — 액션 8개

A2019DemoPackage 작업에 대한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Comment On Tweet** (`CommentOnTweet`) | Comment on Existing Tweet | Session Name(TEXT*), Original Tweet ID(TEXTAREA*), Comment Body(TEXTAREA*), Comment Media(FILE) | STRING |
| **Create Session** (`createsession`) | Creates a session for the Twitter API | Session Name(TEXT*), Consumer Key(CREDENTIAL*), Consumer Secret(CREDENTIAL*), Access Token(CREDENTIAL*), Token Secret(CREDENTIAL*) | STRING |
| **Follow User** (`FollowUser`) | Follow User on Twitter | Session Name(TEXT*), Twitter Handle for User to Follow(TEXT*) | STRING |
| **Like Tweet** (`LikeTweet`) | Like Tweet based on Status ID | Session Name(TEXT*), Status ID of Tweet to Like(TEXTAREA*) | STRING |
| **Retweet** (`Retweet`) | Retweet by statusID | Session Name(TEXT*), StatusID to Retweet(TEXTAREA*) | STRING |
| **Retweet with Comment** (`RetweetWithComment`) | Retweet an existing tweet with a custom comment | Session Name(TEXT*), Original Tweet ID or URL(TEXTAREA*), Comment to add when retweeting(TEXTAREA*) | STRING |
| **Search for Tweets** (`SearchForTweets`) | Search for Tweets matching a query | Session Name(TEXT*), Search Query(TEXT*), Max Search Result(NUMBER*) | TABLE |
| **Tweet** (`setstatus`) | Update Status on Twitter (Tweet) | Session Name(TEXT*), Tweet Body(TEXTAREA*), Tweet Media(FILE) | STRING |

### PDF (`PDF`) — 액션 8개

PDF 파일 작업을 수행합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **문서 비밀번호 해독** (`decryptDocument`) | PDF 파일을 비밀번호 해독합니다. | PDF 경로(FILE*), 사용자/소유자 비밀번호(CREDENTIAL), 비밀번호 해독된 PDF 파일을 다른 이름으로 저장(FILE*), 파일을 같은 이름으로 덮어쓰기(CHECKBOX*) | DICTIONARY |
| **문서 비밀번호화** (`encryptDocument`) | PDF 파일을 비밀번호화합니다. | PDF 경로(FILE*), 사용자 비밀번호:(CREDENTIAL), 소유자 비밀번호:(CREDENTIAL), 적용할 사용자 권한(GROUP), 비밀번호화 수준(RADIO*: RC4 40비트/RC4 128비트/AES 128비트), 비밀번호화된 PDF 파일을 다른 이름으로 저장(FILE*), 파일을 같은 이름으로 덮어쓰기(CHECKBOX*) | DICTIONARY |
| **필드 추출** (`extractField`) | PDF 파일에서 필드를 추출하여 문자열 변수에 지정 | PDF 경로(FILE*), 파일 보호됨(SELECT*: 예/아니요), 필드 추출(ENTRYLIST*: None/유형/캡처된 텍스트/None/None/None…) | DICTIONARY |
| **이미지 추출** (`extractImage`) | PDF 문서를 이미지 파일로 저장합니다. | PDF 경로(FILE*), 사용자 비밀번호:(CREDENTIAL), 소유자 비밀번호:(CREDENTIAL), 페이지 범위(RADIO*: 모든 페이지/페이지), 변환할 이미지의 유형(SELECT*: TIFF/BMP/JPEG/GIF/PNG/WMF…), 폴더 경로(TEXT*), 파일 프리픽스(TEXT*), 파일을 같은 이름으로 덮어쓰기(CHECKBOX*), X 해상도(dpi)(NUMBER*), Y 해상도(dpi)(NUMBER*), 이미지 출력(RADIO*: 색상/그레이스케일) | DICTIONARY |
| **텍스트 추출** (`extractText`) | PDF 파일로부터 텍스트를 추출하여 텍스트 파일 안으로 저장합니다. | PDF 경로(FILE*), 사용자 비밀번호:(CREDENTIAL), 소유자 비밀번호:(CREDENTIAL), 텍스트 유형(RADIO*: 일반 텍스트로/구조화 텍스트), 페이지 범위(RADIO*: 모든 페이지/페이지), 텍스트 파일로 데이터 내보내기(FILE*), 파일을 같은 이름으로 덮어쓰기(CHECKBOX*) | DICTIONARY |
| **속성 가져오기** (`getProperty`) | PDF 문서의 속성을 가져옵니다 | PDF 경로(FILE*), 파일 보호됨(SELECT*: 예/아니요) | DICTIONARY |
| **문서 병합** (`mergeDocument`) | 여러 개의 PDF 문서를 하나의 PDF 문서에 병합합니다. | PDF 문서(ENTRYLIST*: 파일/None/None/페이지/특정 페이지), 출력 파일 경로(FILE*), 기존 파일 덮어쓰기(CHECKBOX*) | - |
| **문서 분할** (`splitDocument`) | PDF 파일을 여러 PDF 파일로 분할합니다. | PDF 경로(FILE*), 사용자 비밀번호:(CREDENTIAL), 소유자 비밀번호:(CREDENTIAL), 출력 파일 생성 옵션(RADIO*: 추출된 PDF당 페이지 수/선택한 페이지가 있는 단일 파일/빈 페이지로 분리/파일당 북마크 레벨), 폴더 경로(TEXT*), 파일 프리픽스(TEXT*), 파일을 같은 이름으로 덮어쓰기(CHECKBOX*) | DICTIONARY |

### 날짜 시간 (`Datetime`) — 액션 7개

날짜/시간 변수의 값 업데이트 및 비교와 같은 날짜/시간 값에 대해 다양한 작업을 수행할 수 있습니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **추가** (`add`) | 날짜/시간 변수의 값을 지정된 시간 값 및 단위만큼 증가시킵니다. 예를 들어 날짜/시간 변수 값을 3시간 또는 3일 늘립니다. | 소스 날짜 및 시간 변수(DATETIME*), 추가할 시간 값(NUMBER*), 추가할 시간 단위(SELECT*: 밀리세컨드/초/분/시간/일/주…) | DATETIME |
| **지정** (`assign`) | 선택한 날짜/시간 형식으로 문자열 변수를 할당하거나 날짜/시간 값을 수동으로 할당하거나 기존 날짜/시간 변수를 날짜/시간 변수에 할당합니다. | 소스 날짜 및 시간 변수/값 선택(RADIO*: constant/변수) | DATETIME |
| **이후임** (`isAfter`) | 두 날짜/시간 변수를 비교하고 원본 변수의 값이 비교 변수의 값 이후인지 확인하고 출력을 부울 변수에 저장합니다. | 소스 날짜 및 시간 변수(DATETIME*), 비교할 날짜 및 시간 변수(DATETIME*) | BOOLEAN |
| **이전임** (`isBefore`) | 두 날짜/시간 변수를 비교하고 원본 변수의 값이 비교 변수의 값 이전인지 확인하고 출력을 부울 변수에 저장합니다. | 소스 날짜 및 시간 변수(DATETIME*), 비교할 날짜 및 시간 변수(DATETIME*) | BOOLEAN |
| **동등함** (`isEqual`) | 두 날짜/시간 변수를 비교하고 원본 변수의 값이 비교 변수의 값과 같은지 확인하고 출력을 부울 변수에 저장합니다. | 소스 날짜 및 시간 변수(DATETIME*), 비교할 날짜 및 시간 변수(DATETIME*) | BOOLEAN |
| **빼기** (`subtract`) | 날짜/시간 변수의 값을 지정된 시간 값 및 단위만큼 감소시킵니다. 예를 들어 날짜/시간 변수 값을 3시간 또는 3일 줄입니다. | 소스 날짜 및 시간 변수(DATETIME*), 뺄 시간 값(NUMBER*), 뺄 시간 단위(SELECT*: 밀리세컨드/초/분/시간/일/주…) | DATETIME |
| **To 문자열** (`toString`) | 날짜/시간 값을 문자열 값으로 변환하고 미리 정의된 형식을 선택하거나 출력 값에 대한 사용자 지정 형식을 지정할 수 있습니다. | 소스 날짜 및 시간 변수(DATETIME*), 날짜 시간 형식 선택(RADIO*: 형식/사용자 지정 형식) | STRING |

### Google Maps (`GoogleMaps`) — 액션 7개

Google Maps Actions

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Distance** (`Distance`) | Returns dictionary variable with distance results | Session name(TEXT*), Origin(TEXT*), Destination(TEXT*), Distance units(RADIO*: Imperial/Metric), Travel mode(SELECT: Driving/Walking/Bicycling/Transit) | DICTIONARY |
| **End Session** (`End Session`) | Session End | Session name(TEXT*) | - |
| **Geocode** (`Geocode`) | Returns dictionary variable with coordinates results | Session name(TEXT*), Address(TEXT*) | DICTIONARY |
| **Get Map Image** (`Get Map Image`) | Returns image of map based on search criteria | Session name(TEXT*), Center (location) of the map(TEXT*), Zoom level(TEXT*), Type(RADIO*: Roadmap/Satellite/Terrain/Hybrid), Horizontal pixel size(TEXT*), Horizontal pixel size(TEXT*), File Path for .png output(FILE*) | - |
| **Search Places** (`Search Places`) | Returns table variable with list of places | Session name(TEXT*), Search Terms(TEXT*) | TABLE |
| **Start Session** (`Start Session`) | Enter API Key to authenticate with Google API | Session name(TEXT*), API Key(CREDENTIAL*) | - |
| **Time Zone** (`Time Zone`) | Returns string variable with time zone results | Session name(TEXT*), Coordinates(GROUP) | STRING |

### 부울 (`Boolean`) — 액션 6개

부울 작업을 수행하기 위한 활동을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **지정** (`assign`) | 소스 부울 변수의 값 또는 사용자 지정 값을 대상 부울 변수 지정 | 소스 부울 변수/ 값 선택(RADIO*: 상수 값/변수 값) | BOOLEAN |
| **비교** (`compareTo`) | 부울 값 비교 | 첫 번째 부울 변수 선택(VARIABLE*), 두 번째 부울 변수 선택(VARIABLE*) | NUMBER |
| **동일** (`equalTo`) | 두 개의 부울 변수가 동일한지 확인하고 결과를 다른 부울 변수에 지정 | 첫 번째 부울 변수 선택(VARIABLE*), 두 번째 부울 변수 선택(VARIABLE*) | BOOLEAN |
| **반전** (`invert`) | 부울 변수의 값을 반전, 즉 참을 거짓으로, 거짓을 참으로 반전시키고 출력을 변수에 지정(동일하거나 다름) | 반전시킬 부울 변수 선택(BOOLEAN*) | BOOLEAN |
| **숫자로** (`toNumber`) | 부울 변수/값을 수로 변환합니다. ‘참’은 ‘1’로 변환되고 ‘거짓’은 ‘0’으로 변환됩니다. | 부울 변수 선택(VARIABLE*) | NUMBER |
| **To 문자열** (`toString`) | 부울 값을 문자열로 변환하여 문자열 변수에 지정 | 부울 변수 선택(VARIABLE*) | STRING |

### 수 (`Number`) — 액션 5개

수 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **지정** (`assignToNumber`) | 사용자 지정 수를 숫자 변수에 지정합니다. | 소스 문자열 변수/값 선택(NUMBER*) | NUMBER |
| **감소** (`decrement`) | 지정된 값만큼 수 감소 | 수 입력(NUMBER*), 감소 값 입력(NUMBER*) | NUMBER |
| **증가** (`increment`) | 지정된 값만큼 수 증가 | 수 입력(NUMBER*), 증가 값 입력(NUMBER*) | NUMBER |
| **무작위 ** (`randomNumber`) | 숫자 변수에 임의의 숫자를 할당합니다. | 범위 시작:(NUMBER*), 범위 끝:(NUMBER*) | NUMBER |
| **To 문자열** (`toString`) | 사용자가 지정한 수를 문자열로 변환 | 수 입력(NUMBER*), 소숫점 후 자릿수 입력(숫자 형식)(NUMBER*) | STRING |

### 시스템 (`System`) — 액션 5개

시스템의 잠금, 로그오프, 재시작 및 종료 작업을 자동화합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **컴퓨터 잠금** (`lock`) | Bot이 실행되는 로컬 시스템을 잠급니다. | - | - |
| **로그오프** (`logoff`) | Bot이 실행되는 로컬 시스템의 현재 사용자 세션에서 로그오프합니다. | - | - |
| **재시작** (`restart`) | Bot이 실행되는 로컬 시스템을 다시 시작합니다. | - | - |
| **종료** (`shutdown`) | Bot이 실행되는 로컬 시스템을 끕니다. | - | - |
| **환경 변수 받기** (`systemInformation`) | 지정된 로컬 시스템에서 모든 환경 변수를 검색하여 변수에 할당합니다. | variableOption(SELECT: 환경 변수 목록/변수) | STRING |

### 사전 (`Dictionary`) — 액션 5개

사전 작업을 수행하기 위한 활동을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **지정** (`assign`) | 소스 사전 변수의 값을 대상 사전 변수에 지정 | 소스 사전 변수 선택(VARIABLE*) | DICTIONARY |
| **가져오기** (`get`) | 사전에서 키와 관련된 항목을 반환합니다. | 사전 변수(VARIABLE*), 키(TEXT*) | ANY |
| **놓기** (`put`) | 사용자가 사전 변수를 보고 삽입할 수 있게 허용합니다. | 사전 변수(VARIABLE*), 이 키에 연결(TEXT*), 새 값(VARIABLE*) | ANY |
| **삭제** (`remove`) | 사용자가 사전 변수를 보고 삭제할 수 있게 허용합니다. | 사전 변수(VARIABLE*), 키(TEXT*) | ANY |
| **크기** (`size`) | 사전의 항목의 수를 반환합니다. | 사전 변수(VARIABLE*) | NUMBER |

### REST Web Services (`Rest`) — 액션 5개

웹 서비스 작업을 수행하기 위한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **삭제 방법** (`restDelete`) | 삭제 방법은 요청 URI에 의해 식별된 정보(개체의 형태로)를 삭제합니다. | URI(TEXT*), 인증 모드(SELECT*: 인증 없음/기본/로그인한 AD 사용자/NTLM 인증(AD 사용자)), 머리글(GROUP) | DICTIONARY |
| **가져오기 방법** (`restGet`) | 가져오기 방법은 요청 URI에 의해 식별된 정보(개체의 형태로)를 가져옵니다(읽음). | URI(TEXT*), 인증 모드(SELECT*: 인증 없음/기본/로그인한 AD 사용자/NTLM 인증(AD 사용자)), 머리글(GROUP) | DICTIONARY |
| **패치 방법** (`restPatch`) | 패치 방법은 요청 URI에 의해 식별된 정보(개체의 형태로)를 일부 바꿉니다. | URI(TEXT*), 인증 모드(SELECT*: 인증 없음/기본/로그인한 AD 사용자/NTLM 인증(AD 사용자)), 머리글(GROUP) | DICTIONARY |
| **게시 방법** (`restPost`) | 게시 방법(생성)은 요청 URI에 의해 식별된 정보(개체의 형태로)에 추가합니다. | URI(TEXT*), 인증 모드(SELECT*: 인증 없음/기본/로그인한 AD 사용자/NTLM 인증(AD 사용자)), 머리글(GROUP) | DICTIONARY |
| **올리기 방법** (`restPut`) | 올리기 방법은 요청 URI에 의해 식별된 정보(개체의 형태로)를 바꿉니다. | URI(TEXT*), 인증 모드(SELECT*: 인증 없음/기본/로그인한 AD 사용자/NTLM 인증(AD 사용자)), 머리글(GROUP) | DICTIONARY |

### HTML Parser (`HTMLParser`) — 액션 5개

Provides actions for HTML parsing.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Advanced Search with RegEx** (`regexsearch`) | RegEx Search by Attribute Value or Text Value | HTML(TEXT*), RegEx(TEXT*), Attribute Name(TEXT), Query Type(SELECT*: By Attribute Value/By Text Value) | LIST |
| **Get HTML Content** (`htmlcontent`) | Get HTML text from a Web site or File | HTML Source(TEXT*), Source Type(SELECT*: URL/File) | STRING |
| **Convert HTML to XML** (`htmltoxml`) | Converts HTML data to XML | HTML(TEXT*) | STRING |
| **Search with Selector** (`searchwithselector`) | Advanced Search with Selector | HTML(TEXT*), Selector(TEXT*) | LIST |
| **Simple Search for Elements** (`simplesearch`) | Simple Search for Elements by ID, Tag, Text, Class or Attribute | HTML(TEXT*), Query(TEXT*), Query Type(SELECT*: By ID/By Tag/By Text/By Class/By Attribute) | LIST |

### Kore AI NLP (`Kore AI NLP`) — 액션 5개

Kore AI NLP

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **End Session** (`EndSession`) | End Session | Session name(TEXT*) | - |
| **Start Session** (`StartSession`) | Start Session | Session name(TEXT*), Webhook Client ID(CREDENTIAL*), Webhook Client Secret(CREDENTIAL*), Webhook URL(TEXT*) | - |
| **Convert Entities To Table** (`Convert Entities To Table`) | Convert Entities To Table | Session name(TEXT*), Raw Text to Convert(TEXT*), Separator between Entities(TEXT*), Separator between Entity Type and Entity Value(TEXT*) | TABLE |
| **Run Model on Text** (`Run Model on Text`) | Run Model on Text | Session name(TEXT*), Text To Extract Entities From(TEXT*) | STRING |
| **Sanitize Text** (`Sanitize Text`) | Sanitize Text | Text To Extract(TEXT*) | STRING |

### Locale (`Locale`) — 액션 5개

Provides actions for localization operations.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Changes Date String Format** (`changedate`) | Changes the Date Format of String | Date String(TEXT*),  Source Format(TEXT*),  Source Locale(TEXT*),  Target Format(TEXT*),  Target Locale(TEXT*) | STRING |
| **Converts Date to String ** (`datetostring`) | Converts a Date to a String | Date (DATETIME*), Target Format(TEXT*), Locale(TEXT*) | STRING |
| **Converts String to Number ** (`stringtonumber`) | Convert a String to a Number | Number String(TEXT*), Locale(TEXT*) | NUMBER |
| **Get Type of Value** (`valuetype`) | Get Type of Value | Variable(VARIABLE*) | STRING |
| **Converts String to Date ** (`stringtodate`) | Converts a String to a Date | Date String(TEXT*), Source Format(TEXT*), Locale(TEXT*) | DATETIME |

### Salesforce (`Salesforce`) — 액션 5개

Actions for interacting with Salesforce Objects and the Salesforce API

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Authenticate** (`authenticate`) | Authenticates via the Salesforce API to generate an Access Token | Session Name(TEXT*), Base URL for your Salesforce Env(CREDENTIAL*), Client ID(CREDENTIAL*), Client Secret(CREDENTIAL*), Salesforce Username(CREDENTIAL*), Salesforce Password(CREDENTIAL*) | STRING |
| **Delete Object** (`deleteobject`) | Delete a Salesforce Object | Session Name(TEXT*), Object Type for Deletion(TEXT*), Object ID for object targetted for deletion(TEXT*) | STRING |
| **Execute SOQL** (`execute_soql`) | Execute a SOQL Query and get a response as JSON | Session Name(TEXT*), SOQL Query to execute(TEXT*) | STRING |
| **Insert Object** (`insertobject`) | Create Salesforce Objects | Session Name(TEXT*), Object Type for Updating(TEXT*), Value(s) to Insert (as key value pairs)(DICTIONARY*) | STRING |
| **Update Object** (`updateobject`) | Update Object by ObjectID and Type | Session Name(TEXT*), Object Type for Updating(TEXT*), ObjectID of object to update(TEXT*), Value(s) To update (as key value pairs)(DICTIONARY*) | STRING |

### A2019DemoPackage (`FileFolderAttributes`) — 액션 5개

A2019DemoPackage 작업에 대한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Files in Folder Deep** (`AllFileInSubDirs`) | Returns all files in directory and subdirectories of provided folder | Path of Folder to Evaluate(FILE*) | LIST |
| **File Attributes** (`FileAttributes`) | Returns a Dictionary of file attributes for the provided file path | Path of file to evaluate(FILE*) | DICTIONARY |
| **Files in Folder** (`FilesinFolder`) | Returns an ordered list of all files (including hidden files) in a directory by customizable sort order | Folder Path to Evaluate(FILE*), File sorting method(SELECT*: By Date - Desc/By Date - Asc/By Name - Desc/By Name - Asc/By Size - Asc/By Size - Desc) | LIST |
| **Folder Attributes** (`FolderAttributes`) | Returns attributes for the provided Folder path | Path of Folder to Evaluate(FILE*) | DICTIONARY |
| **Folders in Folder** (`FoldersinFolder`) | Returns a list of direct subfolders of the provided parent folder | Folder Path to Evaluate(FILE*) | LIST |

### 오류 처리기 (`ErrorHandler`) — 액션 4개

Bot에서 오류를 처리하는 명령을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Try** (`try`) | 예외로 실패할 수 있는 일련의 명령을 시도합니다. | - | - |
| **Catch** (`catch`) | Try 내 명령이 예외로 실패할 경우 일련의 명령을 실행합니다. | 예외(EXCEPTION*), 오류 발생 시, 다음 작업을 계속합니다.(CHECKBOX) | - |
| **Finally** (`finally`) | Try 또는 Catch가 완료된 후 일련의 명령을 실행합니다. | - | - |
| **Throw** (`throw`) | 오류를 나타내기 위해 예외를 발생시킵니다. | 예외 메시지를 입력합니다.(TEXT), 예외(EXCEPTION*) | - |

### MS Word (`MSWordPackage`) — 액션 4개

Use this package to create and modify MS Word Document

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Add Paragraph** (`AddParagraph`) | Add Paragraph in Existing MS Word Document | Select the Word document(FILE*), Please write paragraph or select variable(TEXT*) | - |
| **Create MS Word Document** (`CreateDocument`) | Create a new MS Word Document | Enter the file path(TEXT*), Enter the filename(TEXT*), Write paragraph or select variable(TEXT) | - |
| **Insert Text** (`Bookmark`) | Insert Text at Bookmark Position in MS Word | Select the Word document(FILE*), Enter Bookmark Name(TEXT*), Enter Text to be Inserted at Bookmark position(TEXT*) | - |
| **Replace Text** (`ReplaceText`) | Replace Existing text in MS Word Document | Select the Word document(FILE*), Enter Text to be replaced(TEXT*), Enter new Text(TEXT*) | - |

### DataRobot Models (`DataRobot Models`) — 액션 4개

DataRobot Integration with Model Deployments

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **End Session** (`End Session`) | Session End | Session name(TEXT*) | - |
| **Get Best Predictions** (`Get Best Predictions`) | Get Best Predictions for Data Set | Session name(TEXT*), DataRobot Key(TEXT*), Deployment ID(TEXT*), Prediction Type(SELECT*: Multiclass/Binary/Binary with Explanations), Data Set File (.csv (with headers) or .json)(FILE*) | LIST |
| **Get Deployments** (`Get Deployments`) | Retrieves Deployment Names, IDs, DataRobot Key and URL for generating predictions | Session name(TEXT*) | LIST |
| **Start Session** (`Start Session`) | Enter Username/PW or API Key to authenticate with DataRobot | Session name(TEXT*), Inference Base URL(TEXT*), Account Base URL(TEXT*), API Key(CREDENTIAL) | - |

### DLL (`DLL`) — 액션 4개

DLL을 열고 DLL 함수를 실행하고 DLL을 닫을 수 있습니다

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **닫기** (`Close`) | 해당 DLL 참조를 닫습니다 | DLL 세션(TEXT*) | - |
| **열기** (`Open`) | 실행 작업에 사용할 수 있는 사용자 지정 DLL 참조를 추가합니다. 현재 C# DLL이 지원됩니다. | 새 DLL 세션(TEXT*), File path(FILE*) | - |
| **기능(레거시) 실행** (`Run function`) | 사전 변수를 통해 받은 매개변수를 이용하여 DLL 기능을 실행합니다. 이 작업의 비레거시 버전을 사용할 것을 권장합니다 | DLL 세션(TEXT*), 네임스페이스를 입력합니다(TEXT*), 클래스 이름을 입력합니다(TEXT*), 실행할 함수의 이름을 입력합니다(TEXT*), 함수에 대한 매개변수(VARIABLE) | ANY |
| **함수 실행** (`RunCSharpDLL_V1`) | 매개변수를 활용해 특정 DLL 함수를 실행합니다 | Get DLL details(DESKTOPOPERATIONBUTTON), DLL 세션(TEXT*), 네임스페이스를 입력합니다(TEXT*), 클래스 이름을 입력합니다(TEXT*), 실행할 함수의 이름을 입력합니다(TEXT*), 입력 매개변수(ENTRYLIST: 매개변수 이름/매개변수 유형/매개변수 값) | ANY |

### JSON Object Manager (`JSONHandler`) — 액션 3개

Queries JSON objects and returns strings

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Initialize** (`Initialize`) | Queries JSON objects and returns strings | Provide a session name for future access(TEXT*), Provide a properly formatted JSON Object string(TEXT*) | STRING |
| **Query** (`Query`) | Queries JSON object with dot notation | Session Name(TEXT*), JSON query string(TEXT*) | STRING |
| **Set** (`Set`) | Modify a value on a JSON key | Provide a session name for access(TEXT*), The path to a JSON element, must be definite(TEXT*), New key value to use(TEXT*) | STRING |

### If (`If`) — 액션 3개

If 및 else 동작입니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **If** (`if`) | 조건이 참인 경우 동작의 순서 실행 | 조건(CONDITIONAL*) | - |
| **Else If** (`elseIf`) | 이전 조건이 거짓이고 이 조건이 참인 경우 동작의 순서 실행 | 조건(CONDITIONAL*) | - |
| **Else** (`else`) | 이전 조건이 거짓인 경우 동작의 순서 실행 | - | - |

### 화면 (`Screen`) — 액션 3개

애플리케이션 창, 전체 화면 또는 열려 있는 활성 창의 영역을 캡처하는 프로세스를 자동화하고 지정된 위치에 이미지 형식으로 저장합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **캡처 영역** (`captureArea`) | 애플리케이션 창 영역의 스크린샷을 캡처합니다. | 창 선택(WINDOW), 이미지를 저장할 파일 경로(FILE*), 파일 덮어쓰기(CHECKBOX*) | - |
| **바탕화면 캡처** (`captureDesktop`) | 전체 바탕 화면의 이미지를 캡처합니다. | 이미지를 저장할 파일 경로(FILE*), 파일 덮어쓰기(CHECKBOX*) | - |
| **창 캡처** (`captureWindow`) | 열려 있는 애플리케이션 창을 캡처합니다. | 창 제목(WINDOW*), 이미지를 저장할 파일 경로(FILE*), 파일 덮어쓰기(CHECKBOX*) | - |

### 태스크 봇 (`TaskBot`) — 액션 3개

TaskBot을 실행합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **일시 중지** (`pauseTask`) | 현재 처리 중인 태스크를 일시 중지합니다. | - | - |
| **실행** (`runTask`) | 선택한 TaskBot을 실행합니다. | 실행할 TaskBot(TASKBOT*), Repetition(SELECT: 반복하지 않음/N회 반복/반복 기간/Repeat until stopped by user), 사이 시간(CHECKBOX), 에러 발생 시 다음 반복 지속(CHECKBOX) | DICTIONARY |
| **중지** (`stopTask`) | 현재 처리 중인 태스크를 중지합니다. | - | - |

### 루프 (`Loop`) — 액션 3개

일련의 동작을 반복합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **루프** (`loop.commands.start`) | 중단까지 반복적으로 동작 반복 | Loop Type(RADIO*: 반복자/동안) | - |
| **계속** (`loop.commands.continue`) | 현재 반복을 종료하고 루프의 다음 반복으로 계속 | - | - |
| **중단** (`loop.commands.break`) | 현재 루프를 종료하고 루프 뒤의 다음 동작으로 이동 | - | - |

### 레코더 (`Recorder`) — 액션 3개

이 패키지는 객체 작업을 수행하는 데 사용할 수 있습니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **캡처** (`capture`) | 이 명령은 객체의 클릭 이벤트를 시뮬레이션하는 데 사용할 수 있습니다. | 객체 세부정보(UIOBJECT*: 버튼:/텍스트 상자/텍스트 상자/텍스트 상자/테이블/테이블…), 제어 대기(NUMBER*) | ANY |
| **Recorder** (`recorder`) | This command can be used to record a object's event. | CR Endpoint(TEXT), Window(WINDOW), Bot file id(NUMBER), Secure Recording enabled flag(BOOLEAN) | - |
| **창 크기 조정** (`resize`) | 이 명령은 창의 크기를 아래와 같이 조정할 때 사용할 수 있습니다. | 창(WINDOW*), 폭:(NUMBER*), 높이:(NUMBER*) | - |

### Twilio (`Twilio`) — 액션 2개

Send SMS and make voice calls using Twilio API

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Make a Call** (`Place a call from Twilio`) | Outgoing calls using Twilio Account | SID(CREDENTIAL*), Auth Token(CREDENTIAL*), Sender Number(TEXT*), Recipient Number(TEXT*), Message Body(TEXT*) | STRING |
| **Send SMS** (`Send SMS from Twilio`) | Sends SMS using Twilio API | SID(CREDENTIAL*), Auth Token(CREDENTIAL*), Sender Number(TEXT*), Recipient Number(TEXT*), Message Body(TEXT*) | STRING |

### 파일에 기록 (`LogToFile`) — 액션 2개

데이터가 있는 로그 파일을 생성합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **파일에 텍스트 기록** (`logToFile`) | TaskBot이 실행될 때 발생하는 이벤트에 대한 데이터로 로그 파일을 생성합니다. | 파일 경로(FILE*), 기록할 텍스트 입력(TEXTAREA*), 타임스탬프 추가(CHECKBOX*), 기록 시(RADIO*: 기존 로그 파일에 추가/기존 파일 덮어쓰기), 인코딩(SELECT*: ANSI/UNICODE/UTF8/UTF-16LE) | - |
| **파일에 변수 기록** (`logVariablesToFile`) | 사용자 정의 변수로 로그 파일 생성 | 출력 파일 경로(FILE*), 기록 시(RADIO*: 기존 로그 파일에 추가/기존 파일 덮어쓰기), 타임스탬프 추가(CHECKBOX*), 기록할 변수(VARIABLEMAP2*) | - |

### DMN Engine (`DMNEngine`) — 액션 2개

Embedded DMN Engine

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Evaluate DMN Rules Code** (`evaldmnrulescode`) | Evaluate DMN Rules as Code String | Rule Set(TEXT*), Dictionary with input values(DICTIONARY*), DMN Code(CODE*) | LIST |
| **Evaluate DMN Rules File** (`evaldmnrulesfile`) | Evaluate DMN Rules in a Camunda DMN file | DMN File(FILE*), Dictionary with input values(DICTIONARY*), Rule Set(TEXT*) | LIST |

### String_Diff (`String_Diff`) — 액션 2개

Compare two strings.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Diff** (`String_Diff`) | Compare two strings | First string(LIST*), Second string(LIST*) | LIST |
| **List Demo** (`ListTypeDemo`) | Sample action showing how to display a List | Please enter the source(LIST*), Please enter the index(NUMBER*) | ANY |

### 분석... (`Analyze`) — 액션 2개

본 패키지는 분석용으로 사용할 수 있습니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **닫기** (`CloseTransaction`) | 트랜잭션 닫기 | 트랜잭션 이름(TEXT*), 비즈니스 트랜잭션 상태(TEXT*), 사전 변수(VARIABLEMAP*) | - |
| **열기** (`OpenTransaction`) | 트랜잭션 열기 | 트랜잭션 이름(TEXT*) | - |

### 코멘트 (`Comment`) — 액션 1개

자동화 태스크 목록에 사용자 지정 코멘트를 추가하여 논리에 대한 추가 정보를 제공합니다. 이 코멘트는 로직 실행 시 무시됩니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **코멘트** (`Comment`) | 자동화 태스크 목록에 사용자 지정 코멘트를 추가하여 논리에 대한 추가 정보를 제공합니다. 이 코멘트는 로직 실행 시 무시됩니다. | 코멘트(TEXTAREA*) | - |

### 메시지 상자: (`MessageBox`) — 액션 1개

메시지 상자를 표시합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **메시지 상자:** (`messageBox`) | 메시지 상자 표시 | 메시지 상자 창 제목 입력(TEXT*), 표시할 메시지 입력(TEXTAREA*), 라인 뒤 스크롤바(NUMBER*), 뒤 메시지 상자 닫기(CHECKBOX) | - |

### 단계 (`Step`) — 액션 1개

단계 작업

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **단계** (`step`) | 일련의 명령을 실행합니다. | 제목(TEXT) | - |

### System Variables Package (`SystemVariablesPackage`) — 액션 1개

Provides additional system variables related to the bot runner machine which can allow for more dynamic bot builds

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Get System Variables** (`GetSystemVariables`) | Return Various System Values | Select Variable to Return(SELECT: Get User Name/Get User Home Dir/Get OS Name/Get OS Version/Get OS Architecture/Get Current Working Dir…) | STRING |

### CharaCode Converter (`A2019DemoPackage`) — 액션 1개

Provides actions to convert character code in file.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Convert Characode** (`Convert_characode`) | Provides actions to convert character code in file. | Select file to convert(FILE*), Select language code(SELECT*: Shift-JIS/UTF-8/EUC-JP), Select file to convert(TEXT*), Select language code(SELECT*: Shift-JIS/UTF-8/UTF-8 BOM/EUC-JP) | - |

### Dictionary Demo Package (`DictionaryDemo`) — 액션 1개

Sample package to demo dictionary return

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Values to Dictionary** (`ValuestoDictionary`) | Demo package to demonstrate returning values in a dictionary data type | String to return in Dictionary(TEXT*), Number to return in Dictionary(NUMBER*) | DICTIONARY |

### Text Diff (`Text_Diff`) — 액션 1개

Compare two list data.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Diff** (`String_Diff`) | Compare strings in two lists. | First list of strings(LIST*), Second list of strings(LIST*) | LIST |

### Math (`Math`) — 액션 1개

Provides Math actions

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Math Expression** (`mathevaluate`) | Math Expression Parser | Expression(TEXT*), Precision(NUMBER*) | STRING |

### Credential Manager (`Credential Manager`) — 액션 1개

Access Credential Vault values anywhere within your bot

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Read Credential** (`ReadCredential`) | Read Credential from Vault | Credential to Read from Vault(CREDENTIAL*) | STRING |

### A2019DemoPackage (`FileDetails`) — 액션 1개

A2019DemoPackage 작업에 대한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **File Size** (`GetFileDetails`) | Returns the size of selected file in bytes | Select a File for Anaylsis(FILE*) | NUMBER |

### Returning A List (`ReturningAListDemo`) — 액션 1개

Sample Code for Returning a List

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Return a List** (`returnalist`) | Returns a list of the provided strings | Field 1 for List(TEXT*), Field 2 for List(TEXT*) | LIST |

### A2019DemoPackage (`Hexadecimal`) — 액션 1개

A2019DemoPackage 작업에 대한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **ToPdf** (`to_pdf`) | Convert Hexadecimal value to pdf | Hexadecimal(TEXT*), OutputFolder(TEXT*) | STRING |

### A2019DemoPackage (`Imagine Loan Approval`) — 액션 1개

A2019DemoPackage 작업에 대한 동작을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **Analyze Loan** (`loanapproval`) | Loan Approval score provided by pre-trained ML model | Loan Approval ML Model(FILE*), Gender(TEXT*), Marital Status(TEXT*), Number of Dependents(NUMBER*), College Education(TEXT*), Employer(TEXT*), Applicant Monthly Income(NUMBER*), Co-Applicant Monthly Income(NUMBER*), Total Requested Loan Amount(NUMBER*), Total Length of Loan (in Months)(NUMBER*), Existing Credit History(TEXT*), Property Area(TEXT*) | DICTIONARY |

### 애플리케이션 (`Application`) — 액션 1개

애플리케이션 작업을 수행하기 위한 활동을 제공합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **프로그램/파일 열기** (`runApp`) | 프로그램/파일 열기 | 프로그램/파일의 위치(FILE*), 경로에서 시작(TEXT), 매개변수(TEXT) | - |

### 지연 (`Delay`) — 액션 1개

지연 동작을 수행합니다.

| 액션 | 설명 | 파라미터 | 반환 |
|---|---|---|---|
| **지연** (`delay`) | 시간 지정 지연 추가 | 지연 유형(RADIO*: 정규/무작위), 시간 단위(RADIO*: 밀리세컨드/초) | - |

## 봇 예제 (bot_example 20건)

- Untitled
- RSSFeedReaderBot
- HugeExcel_ExcelAdvanced
- HugeExcel_ExcelBasic
- HugeExcel_Database
- Profit Calculation Bot Episode 1
- GeneratePreApprovalLetter
- EvaluateLoan
- Imagine - Employee Anniversaries Table Version
- PreApprovalProcess
- RebuildModel
- Sales Data Analysis
- LetterGenerator
- GoogleVision_IQBot_Qualifier
- AA_Latest_Log_Finder
- SOAPWebservice_Access_Using_DLL
- FileandFolderQuickTip
- Test Bot CX
- Hello World
- OutlookOperationsSampleBot
