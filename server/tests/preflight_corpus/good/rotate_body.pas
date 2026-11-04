// Stand a Y-up STEP model on its pads (proven 2026-11-04).
Obj1 := PCBServer.GetPCBBoardByPath('C:\scratch\scratch.PcbDoc');
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(eComponentBodyObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj3 := Obj2.FirstPCBObject;
if Obj3 <> nil then
begin
    Obj4 := Obj3.Model;
    Obj4.SetState(90, 0, 0, 0);
    Obj3.Model := Obj4;
    Obj3.SetState_FromModel;
    ResultText := '{"height": ' + FloatToStr(CoordToMMs(Obj3.OverallHeight)) + '}';
end;
Obj1.BoardIterator_Destroy(Obj2);
