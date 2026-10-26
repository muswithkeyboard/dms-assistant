

def test_clause_number_ignores_trailing_dot():
    # Зетта хранит номер как в Word («1.2.»), запрос «1.2» и «1.2.» должны находить его одинаково
    import tempfile
    from pathlib import Path

    from dms_core.store.db import KitStore
    from fixtures.synth_kit import make_parsed

    pc = make_parsed("c01")
    pc.clauses[0] = pc.clauses[0].model_copy(update={"number": "7.3."})
    with tempfile.TemporaryDirectory() as d:
        st = KitStore(Path(d) / "kit.sqlite")
        st.save(pc)
        assert [c.number for c in st.clause("c01", number="7.3")] == ["7.3."]
        assert [c.number for c in st.clause("c01", number="7.3.")] == ["7.3."]


def test_unknown_contract_raises_and_path_suffix_is_segment():
    # Ревизия w1: несуществующий договор — KeyError, а не пустой ответ; путь — целыми сегментами «› »
    import tempfile
    from pathlib import Path

    from dms_core.store.db import KitStore
    from fixtures.synth_kit import make_parsed

    pc = make_parsed("c01")
    pc.clauses[0] = pc.clauses[0].model_copy(update={"path": "Программа › Бизнес › 3.1.4"})
    pc.clauses[1] = pc.clauses[1].model_copy(update={"path": "Программа › Бизнес › 1.4"})
    with tempfile.TemporaryDirectory() as d:
        st = KitStore(Path(d) / "kit.sqlite")
        st.save(pc)
        for bad in ("c99", "C01", "gk1"):
            for call in (lambda c: st.clinics(c), lambda c: st.clauses(c), lambda c: st.care(c),
                         lambda c: st.documents(c), lambda c: st.clause(c, number="1")):
                try:
                    call(bad)
                except KeyError:
                    pass
                else:
                    raise AssertionError(f"{bad}: нет KeyError")
        assert [c.path for c in st.clause("c01", path="1.4")] == ["Программа › Бизнес › 1.4"]
        try:
            st.clause("c01", path="  ")
        except ValueError:
            pass
        else:
            raise AssertionError("пустой путь должен давать ValueError")
