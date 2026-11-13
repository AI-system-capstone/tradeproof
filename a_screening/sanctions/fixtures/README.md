# sanctions/fixtures — 가짜 테스트 데이터

이 폴더의 `SDN.CSV`, `ALT.CSV`, `ADD.CSV`는 OFAC SDN 레거시 CSV와 **형식만 같은 가상 데이터**입니다.
이름, 주소, 콜사인, IMO 번호는 모두 지어낸 것이며 실제 제재 대상과 관계가 없습니다.
진짜 파일을 받기 전에도 파이프라인을 돌려 볼 수 있게 넣어 두었습니다.

- 단체 62개, 선박 15척, 개인 10명, 항공기 2대 (개인·항공기는 스크립트가 걸러내는지 확인용)
- 헤더 없음, 빈 값은 `-0-`, 줄 끝 CRLF, 파일 끝 `\x1a` 문자 — 실제 파일 형식을 따름
- `ADD.CSV`는 일부러 cp1252로 저장 (UTF-8이 아닐 때 대체 인코딩으로 읽는지 확인용)
- 이름은 정규화 후 서로 유사도 0.8 이하, 12자 이상이 되도록 만들었음

## 실제 데이터로 바꾸기

1. https://sanctionslist.ofac.treas.gov/Home/static/sdn.html 에서 `SDN.CSV`, `ALT.CSV`, `ADD.CSV`를 내려받습니다.
2. 이 폴더를 덮어쓰지 말고 다른 폴더(예: `sanctions/ofac_raw/`)에 둡니다.
3. 내려받은 폴더를 `--src`로 지정해 실행합니다.

```
python sanctions/build_lists.py --src sanctions/ofac_raw --fetched-at 2026-11-07
```

결과는 `data/sanctions/list-v14.json`, `data/sanctions/list-v15.json`에 저장됩니다.
`--seed`가 같으면 항상 같은 표본이 뽑힙니다.
