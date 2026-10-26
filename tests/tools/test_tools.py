

def test_clinic_name_needs_distinctive_word():
    # Общие слова («клиника», «медицинский центр») не делают выдуманную клинику похожей на настоящую (ревизия w3)
    from dms_core.schemas import ClinicRow
    from dms_core.tools.clinics import name_score

    row = ClinicRow(id="c01.clinics#t0r1", contract_id="c01", doc_id="c01.clinics", program="Стандарт",
                    care_type="Поликлиника", clinic_name="ООО «Медицинский центр «Альфа-Здоровье»»",
                    source_ref="c01.clinics#t0r1")
    assert name_score("Несуществующая клиника Бета", row) == 0
    assert name_score("Альфа-Здоровье", row) == 100
    assert name_score("Альфа Здоровье", row) >= 88
    assert name_score("медицинский центр", row) > 0          # запрос только из общих слов — обычное сходство
