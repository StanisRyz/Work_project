/**
 * Editable calculation rules, ported unchanged from `data/rules.js` of
 * StanisRyz/calculate @ d32eae0. Densities, coefficients and limits live
 * here and nowhere else. Do not "improve" a number without an agreed
 * formula change and a new `CALCULATION_VERSION`.
 */
window.windingCalculator = window.windingCalculator || {};
window.windingCalculator.rules = {
  // Плотность стали, кг/мм³. Применяется к объёму кольца.
  densityKgPerCubicMm: 0.00000763,

  // Базовая скорость: для ленты 0,30 мм — 4,40 сек/мм.
  windingSpeedBase: { thicknessMm: 0.30, secondsPerMm: 4.40 },

  // Обручная геометрия: большой d при малой высоте H усложняет обработку.
  hoopGeometry: {
    referenceRatio: 5,
    coefficient: 0.00015,
    maximum: 2.00,
    diameterTiers: [
      { maximumMm: 500, factor: 1.00 },
      { maximumMm: 600, factor: 1.10 },
      { maximumMm: 700, factor: 1.25 },
      { maximumMm: 800, factor: 1.40 },
      { maximumMm: null, factor: 1.50 }
    ]
  },

  additionalOperations: {
    // Базовое время доп. операций для d >= 80: 0.1 × масса² + 90, сек; ограничение — 180 сек.
    // Для d < 80 используется фиксированная база 40 сек.
    baseTime: { massSquaredCoefficient: 0.1, offsetSeconds: 90, maximumSeconds: 180, innerDiameterThresholdMm: 80, belowThresholdSeconds: 40 },
    // Время калибровки, сек: 12 × диаметр калибровки (мм) + 180. Добавляется к базовому времени.
    calibrationTime: { diameterCoefficientSecondsPerMm: 12, offsetSeconds: 180 },
    // Множитель ширины b, где b в мм.
    width: { lowerBoundaryMm: 30, upperBoundaryMm: 60, belowCoefficient: 0.001, aboveCoefficient: 0.00015 },
    // Множитель толщины ленты t, где t в мм.
    thickness: { referenceMm: 0.3, coefficient: 4 },
    // Множитель массы, где масса в кг; максимум 2.
    mass: { referenceKg: 1, coefficient: 0.000102, maximum: 2 },
    // Множитель высоты навивки, где высота в мм; максимум 1.75.
    height: { referenceMm: 1, coefficient: 0.000045, maximum: 1.75 },
    // Множитель внутреннего диаметра d, где d в мм; для d >= 80 максимум 3.
    innerDiameter: { boundaryMm: 80, lowReferenceMm: 10, lowCoefficient: -0.000102, lowOffset: 1.5, highCoefficient: 0.00000275, highMaximum: 3 }
  }
};
