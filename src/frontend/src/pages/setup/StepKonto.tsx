import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { zodResolver } from '@hookform/resolvers/zod'
import { InfoTooltip } from '../../components/InfoTooltip'
import { validiereIban, ladeOpenIbanDaten, OPEN_IBAN_BANK_FALLBACK, OPEN_IBAN_BIC_FALLBACK, openIbanBankdatenFehlgeschlagen, OPEN_IBAN_LAND_TOOLTIP } from '../../utils/iban'

const schema = z.object({
  name:     z.string().min(1, 'Name ist erforderlich'),
  anbieter: z.string().min(1, 'Bank ist erforderlich'),
  kontoart: z.literal('bank'),
  iban: z
    .string()
    .min(1, 'IBAN ist erforderlich')
    .transform((v) => v.replace(/\s/g, '').toUpperCase())
    .superRefine((v, ctx) => {
      const fehler = validiereIban(v)
      if (fehler) ctx.addIssue({ code: 'custom', message: fehler })
    }),
  bic:      z.string().optional(),
  kontotyp: z.enum(['geschaeftlich', 'mischkonto']),
})

type FormData = z.infer<typeof schema>

type Props = {
  onNext: (data: FormData) => void
  onBack: () => void
  isLoading?: boolean
}

export function StepKonto({ onNext, onBack, isLoading }: Props) {
  const { register, handleSubmit, setValue, getValues, trigger, clearErrors, formState: { errors } } = useForm<FormData>({
    resolver: zodResolver(schema),
    defaultValues: { kontoart: 'bank', kontotyp: 'geschaeftlich' },
  })

  const [bankFehler, setBankFehler] = useState<string | null>(null)
  const [bicFehler, setBicFehler] = useState<string | null>(null)

  async function handleBankLookup() {
    const iban = getValues('iban')?.replace(/\s/g, '').toUpperCase()
    if (!iban) return

    setBankFehler(null)
    setBicFehler(null)

    try {
      const data = await ladeOpenIbanDaten(iban)
      if (data.valid) {
        const bic = data.bankData?.bic ?? ''
        const name = data.bankData?.name ?? ''
        if (!bic || !name) {
          const msg = openIbanBankdatenFehlgeschlagen(iban)
          setBankFehler(msg)
          setBicFehler(msg)
        }
        if (bic) setValue('bic', bic, { shouldDirty: true, shouldValidate: true })
        if (name) setValue('anbieter', name, { shouldDirty: true, shouldValidate: true })
        return
      }
    } catch {
      // bewusst still: die lokale Validierung bleibt maßgeblich
    }

    setBankFehler(OPEN_IBAN_BANK_FALLBACK)
    setBicFehler(OPEN_IBAN_BIC_FALLBACK)
  }

  return (
    <form onSubmit={handleSubmit(onNext)} className="space-y-5">
      <p className="text-sm text-slate-600 dark:text-slate-300">
        Gib dein Geschäftskonto an. Du kannst später weitere Konten hinzufügen.
      </p>

      <div>
        <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">
          Kontobezeichnung <span className="text-red-500">*</span>
        </label>
        <input {...register('name')} placeholder="z.B. Geschäftskonto"
          className="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-slate-700 dark:text-slate-100 dark:placeholder-slate-400" />
        {errors.name && <p className="text-red-500 text-xs mt-1">{errors.name.message}</p>}
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">
          Bank <span className="text-red-500">*</span>
        </label>
        <input {...register('anbieter')} placeholder="z.B. Sparkasse Berlin"
          className="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-slate-700 dark:text-slate-100 dark:placeholder-slate-400" />
        {errors.anbieter && <p className="text-red-500 text-xs mt-1">{errors.anbieter.message}</p>}
        {bankFehler && <p className="mt-1 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 inline-flex items-center gap-1">{bankFehler}{bankFehler.startsWith('Für IBANs') && <InfoTooltip text={OPEN_IBAN_LAND_TOOLTIP} />}</p>}
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">
          IBAN <span className="text-red-500">*</span>
        </label>
        <input
          {...register('iban', {
            onChange: (e) => {
              const el = e.target
              const ns = el.value.slice(0, el.selectionStart ?? el.value.length).replace(/\s/g, '').length
              const raw = el.value.replace(/\s/g, '').toUpperCase()
              const formatted = raw.replace(/(.{4})/g, '$1 ').trimEnd()
              setValue('iban', formatted)
              if (raw.length > 0) {
                trigger('iban')
              } else {
                clearErrors('iban')
              }
              setBankFehler(null)
              setBicFehler(null)
              setTimeout(() => {
                let p = 0, c = 0
                for (let i = 0; i <= formatted.length; i++) {
                  if (c === ns) { p = i; break }
                  if (formatted[i] !== ' ') c++
                }
                el.setSelectionRange(p, p)
              }, 0)
            },
          })}
          placeholder="DE89 3704 0044 0532 0130 00"
          className="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-slate-700 dark:text-slate-100 dark:placeholder-slate-400"
        />
        {errors.iban && <p className="mt-1 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">{errors.iban.message}</p>}
      </div>

      <button type="button" onClick={handleBankLookup}
        className="w-full inline-flex items-center justify-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm font-medium text-blue-700 hover:bg-blue-100 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-300 dark:hover:bg-blue-900">
        <span>↗</span>
        <span>Bankname und BIC aus IBAN per Web</span>
      </button>
      <p className="text-xs text-slate-400 dark:text-slate-500 text-center">Sendet deine IBAN an openiban.com (Fremdanbieter) zur Abfrage</p>

      <div>
        <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">BIC / SWIFT</label>
        <input {...register('bic')} placeholder="COBADEFFXXX"
          className="w-full rounded-lg border border-slate-300 dark:border-slate-600 px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-slate-700 dark:text-slate-100 dark:placeholder-slate-400" />
        {bicFehler && <p className="mt-1 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 inline-flex items-center gap-1">{bicFehler}{bicFehler.startsWith('Für IBANs') && <InfoTooltip text={OPEN_IBAN_LAND_TOOLTIP} />}</p>}
      </div>

      <div>
        <label className="block text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">Kontotyp</label>
        <div className="space-y-2">
          {[
            { value: 'geschaeftlich', label: 'Rein geschäftlich', desc: 'Alle Buchungen sind Betriebseinnahmen/-ausgaben.' },
            { value: 'mischkonto', label: 'Mischkonto', desc: 'Private und geschäftliche Buchungen werden getrennt klassifiziert.' },
          ].map(({ value, label, desc }) => (
            <label key={value} className="flex items-start gap-3 cursor-pointer">
              <input type="radio" {...register('kontotyp')} value={value}
                className="mt-0.5 h-4 w-4 border-slate-300 text-blue-600" />
              <div>
                <span className="text-sm font-medium text-slate-700 dark:text-slate-200">{label}</span>
                <p className="text-xs text-slate-500 dark:text-slate-400">{desc}</p>
              </div>
            </label>
          ))}
        </div>
      </div>

      <div className="pt-2 flex justify-between">
        <button type="button" onClick={onBack}
          className="text-slate-600 dark:text-slate-300 hover:text-slate-800 dark:hover:text-slate-100 font-medium px-4 py-2 rounded-lg transition-colors">
          ← Zurück
        </button>
        <button type="submit" disabled={isLoading}
          className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-medium px-6 py-2 rounded-lg transition-colors">
          {isLoading ? 'Speichern...' : 'Einrichtung abschließen ✓'}
        </button>
      </div>
    </form>
  )
}
