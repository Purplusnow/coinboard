/* 배포 설정.
 *
 * board.json 을 어디서 읽을지 정한다.
 *
 * 기본값(같은 오리진 data/board.json)은 로컬 개발과, 워크플로가 docs/ 로 직접
 * 커밋하는 방식에서 그대로 동작한다.
 *
 * 워크플로를 orphan `data` 브랜치 force-push 방식으로 쓰면(= 저장소 히스토리가
 * 불어나지 않음) 아래 주석을 풀고 본인 계정/저장소로 바꾼다.
 * raw.githubusercontent.com 은 CORS가 열려 있고 max-age=300 으로 캐시된다.
 */
window.BOARD_DATA_URL =
  // "https://raw.githubusercontent.com/<OWNER>/<REPO>/data/board.json";
  "data/board.json";
