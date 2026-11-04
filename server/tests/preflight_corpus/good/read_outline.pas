// Read a polygon outline; the last segment repeats the first.
Obj1 := PCBServer.GetPCBBoardByPath('C:\scratch\scratch.PcbDoc');
Obj2 := Obj1.BoardIterator_Create;
Obj2.AddFilter_ObjectSet(MkSet(ePolyObject));
Obj2.AddFilter_LayerSet(AllLayers);
Obj2.AddFilter_Method(eProcessAll);
Obj3 := Obj2.FirstPCBObject;
List1 := TStringList.Create;
if Obj3 <> nil then
    for I1 := 0 to Obj3.PointCount do
        List1.Add(IntToStr(Obj3.Segments[I1].Kind) + ' ' + FloatToStr(CoordToMMs(Obj3.Segments[I1].vx)));
Obj1.BoardIterator_Destroy(Obj2);
ResultText := List1.Text;
