// Registered rather than bound directly, so the same setup runs on first load
// and again after a live fragment replacement. `claim()` keeps it idempotent.
const initialiseActDefectFormset = (root) => {
    const scope = root || document;
    const actForm = scope.querySelector ? scope.querySelector('.act-form') : null;
    const formset = scope.querySelector ? scope.querySelector('[data-defect-formset]') : null;
    if (!actForm || !formset || !window.qualityFragments.claim(formset)) {
        return;
    }

    const list = formset.querySelector('[data-defect-form-list]');
    const template = formset.querySelector('[data-empty-defect-form]');
    const addButton = formset.querySelector('[data-add-defect]');
    const totalForms = formset.querySelector('input[name$="-TOTAL_FORMS"]');

    if (!list || !template || !addButton || !totalForms) {
        return;
    }

    const setFieldError = (field, message = '') => {
        const label = field.closest('label');
        if (!label) {
            return;
        }
        let error = label.querySelector('[data-client-field-error]');
        if (message) {
            label.classList.add('field--invalid');
            if (!error) {
                error = document.createElement('span');
                error.className = 'client-field-error';
                error.dataset.clientFieldError = '';
                label.append(error);
            }
            error.textContent = message;
        } else {
            label.classList.remove('field--invalid');
            error?.remove();
        }
    };

    const validateField = (field, showRequired = false) => {
        const value = field.value.trim();
        let message = '';
        if (showRequired && field.required && !value) {
            message = 'Заполните поле.';
        } else if (value && field.name.endsWith('kd_designation') && !/^[А-Яа-яЁё0-9.-]+$/.test(value)) {
            message = 'Допустимы только русские буквы, цифры, точки и тире.';
        } else if (value && ['order_number', 'znp_number', 'party_number'].some((name) => field.name.endsWith(name)) && !/^[0-9/-]+$/.test(value)) {
            message = 'Допустимы только цифры, дефис и слэш.';
        } else if (value && field.type === 'number' && (!/^\d+$/.test(value) || Number(value) < 0)) {
            message = 'Введите целое неотрицательное число.';
        }

        if (!message && field.name.endsWith('nonconforming_quantity')) {
            const block = field.closest('.defect-form-block');
            const checkedField = block?.querySelector('input[name$="checked_quantity"]');
            if (checkedField?.value && Number(value) > Number(checkedField.value)) {
                message = 'Количество несоответствующей продукции не может превышать количество проверенной продукции.';
            }
        }
        setFieldError(field, message);
        return !message;
    };

    const validateBlockQuantities = (block) => {
        block?.querySelectorAll('input[name$="checked_quantity"], input[name$="nonconforming_quantity"]')
            .forEach(validateField);
    };

    const getWorkshopSelect = (block) => block?.querySelector('select[name$="-workshop"]') || null;

    // Presentation only. The whole workshop rule set — which fields apply, which
    // are required, which defect types are accepted — is built by
    // `acts/workshops.py` and read from here; the backend validates it again and
    // is the only authority. Never restate a rule in this file.
    const workshopProfiles = (() => {
        const config = formset.querySelector('#defect-workshop-profiles');
        try {
            return config ? JSON.parse(config.textContent) : {};
        } catch (error) {
            return {};
        }
    })();

    const syncDefectTypeOptions = (block, profile) => {
        const select = block.querySelector('select[name$="-defect_type"]');
        if (!select) {
            return;
        }
        const allowedCodes = profile ? profile.defect_types : null;
        let selectedIsHidden = false;
        [...select.options].forEach((option) => {
            const allowed = !option.value
                || !allowedCodes
                || allowedCodes.includes(option.dataset.defectCode);
            option.hidden = !allowed;
            option.disabled = !allowed && Boolean(option.value);
            if (!allowed && option.selected) {
                selectedIsHidden = true;
            }
        });
        if (selectedIsHidden) {
            select.value = '';
        }
    };

    // The date of detection sits in the group the profile names, ordered the way
    // the profile lists its fields: under МП first in «Результат контроля»,
    // before the two quantities; under ПиР after the defect type in «Контроль».
    const syncDetectedAtPlacement = (block, profile) => {
        const detectedAt = block.querySelector('[data-defect-field="detected_at"]');
        const target = profile
            ? block.querySelector(`[data-defect-group="${profile.detected_at_group}"]`)
            : null;
        if (!detectedAt || !target) {
            return;
        }
        const rank = (name) => {
            const index = profile.fields.indexOf(name);
            return index === -1 ? Number.MAX_SAFE_INTEGER : index;
        };
        const successor = [...target.querySelectorAll('[data-defect-field]')].find(
            (label) => label !== detectedAt && rank(label.dataset.defectField) > rank('detected_at'),
        );
        if (successor) {
            target.insertBefore(detectedAt, successor);
        } else {
            target.append(detectedAt);
        }
    };

    const syncWorkshopVisibility = (block) => {
        const select = getWorkshopSelect(block);
        if (!block || !select) {
            return;
        }
        const profile = workshopProfiles[select.value] || null;
        const isChosen = Boolean(profile);
        block.querySelectorAll('[data-defect-collapsible]').forEach((element) => {
            element.hidden = !isChosen;
        });
        // Until a workshop is chosen the collapsible groups above already hide
        // everything but the workshop select itself, and the server-rendered
        // `required` attributes stay as Django wrote them.
        if (isChosen) {
            block.querySelectorAll('[data-defect-field]').forEach((label) => {
                const name = label.dataset.defectField;
                const field = label.querySelector('input, select, textarea');
                const applies = profile.fields.includes(name);
                label.hidden = !applies;
                if (!field) {
                    return;
                }
                field.required = applies && profile.required.includes(name);
                if (!applies && field.value) {
                    field.value = '';
                    setFieldError(field, '');
                }
            });
        }
        const legend = block.querySelector('[data-defect-legend]');
        if (legend) {
            if (!legend.dataset.legendDefault) {
                legend.dataset.legendDefault = legend.textContent;
            }
            legend.textContent = isChosen ? profile.legend : legend.dataset.legendDefault;
        }
        syncDetectedAtPlacement(block, profile);
        syncDefectTypeOptions(block, profile);
    };

    const syncAllWorkshopVisibility = () => {
        list.querySelectorAll('.defect-form-block').forEach(syncWorkshopVisibility);
    };

    const reindexForms = () => {
        const blocks = list.querySelectorAll('.defect-form-block');
        blocks.forEach((block, index) => {
            block.querySelectorAll('input, select, textarea, label').forEach((element) => {
                ['name', 'id', 'for'].forEach((attribute) => {
                    const value = element.getAttribute(attribute);
                    if (!value) {
                        return;
                    }
                    element.setAttribute(attribute, value.replace(/defects-\d+-/g, `defects-${index}-`));
                });
            });
        });
        totalForms.value = blocks.length;
    };

    const syncDefectUi = () => {
        const visibleBlocks = [...list.querySelectorAll('.defect-form-block')]
            .filter((block) => !block.hidden);
        const suffix = visibleBlocks.length === 1 ? 'дефект' : visibleBlocks.length < 5 ? 'дефекта' : 'дефектов';
        const count = formset.querySelector('[data-defect-count]');
        if (count) {
            count.textContent = `${visibleBlocks.length} ${suffix}`;
        }
        visibleBlocks.forEach((block, index) => {
            const title = block.querySelector('[data-defect-title]');
            if (title) {
                title.textContent = `Дефект ${index + 1}`;
            }
        });
    };

    addButton.addEventListener('click', () => {
        const index = Number.parseInt(totalForms.value, 10);
        const html = template.innerHTML.replace(/__prefix__/g, index);
        list.insertAdjacentHTML('beforeend', html);
        totalForms.value = index + 1;
        syncDefectUi();
        syncWorkshopVisibility(list.querySelector('.defect-form-block:last-child'));
    });

    actForm.addEventListener('input', (event) => {
        const field = event.target;
        if (field.matches('input, textarea')) {
            validateField(field, false);
            validateBlockQuantities(field.closest('.defect-form-block'));
        }
    });

    actForm.addEventListener('change', (event) => {
        const field = event.target;
        if (field.matches('select[name$="-workshop"]')) {
            syncWorkshopVisibility(field.closest('.defect-form-block'));
        }
        if (field.matches('select, input[type="date"]')) {
            validateField(field, false);
        }
    });

    actForm.addEventListener('blur', (event) => {
        if (event.target.matches('input, select, textarea')) {
            validateField(event.target, false);
        }
    }, true);

    actForm.addEventListener('submit', (event) => {
        const fields = [...actForm.querySelectorAll('input:not([type="hidden"]), select, textarea')]
            .filter((field) => !field.closest('[hidden]'));
        const isValid = fields.every((field) => validateField(field, true));
        if (!isValid) {
            event.preventDefault();
            actForm.querySelector('.field--invalid input, .field--invalid select, .field--invalid textarea')?.focus();
        }
    });

    list.addEventListener('click', (event) => {
        const removeButton = event.target.closest('[data-remove-defect]');
        if (!removeButton) {
            return;
        }

        const block = removeButton.closest('.defect-form-block');
        const visibleBlocks = [...list.querySelectorAll('.defect-form-block')]
            .filter((formBlock) => !formBlock.hidden);
        if (!block || visibleBlocks.length <= 1) {
            return;
        }

        const deleteField = block.querySelector('input[name$="-DELETE"]');
        if (deleteField) {
            deleteField.checked = true;
            block.hidden = true;
            syncDefectUi();
            return;
        }

        block.remove();
        reindexForms();
        syncDefectUi();
    });

    syncDefectUi();
    syncAllWorkshopVisibility();
};

window.qualityFragments.register("actDefectFormset", initialiseActDefectFormset);
document.addEventListener("DOMContentLoaded", () => initialiseActDefectFormset(document));
