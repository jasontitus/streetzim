# sha256:dfeddd2a65ccfcf6cd72665cae0e3e4a6497cf81248f512db14307e41aab1893
FROM streetzim-review-qa:3.14-native-cached AS qa
# sha256:21008c4cdb1927a34d2392c4f35dfade0a4918be0249831d060a25aca96c32e0
FROM streetzim:python-packer-review-20260930
COPY --from=qa /usr/bin/git /usr/bin/git
COPY --from=qa /usr/lib/git-core /usr/lib/git-core
COPY --from=qa /usr/share/git-core /usr/share/git-core
COPY --from=qa /root/.duckdb /root/.duckdb
RUN git --version && python -c 'import duckdb; duckdb.connect().execute("LOAD spatial")'
